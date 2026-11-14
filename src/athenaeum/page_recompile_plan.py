# SPDX-License-Identifier: Apache-2.0
"""Page recompile: the planner that routes units by id (issue athenaeum#2537,
slice S3 of the page-recompile design, athenaeum#2525). **Shadow only** --
this module writes no page, runs no nightly phase, raises no decision item
and has no undo.

:mod:`athenaeum.page_recompile` (slice S1, issue athenaeum#2534) is the
deterministic front half: it finds an oversized page and cuts its body into
ordered, addressable :class:`~athenaeum.page_recompile.Unit` objects. This
module is the DECISION half. It never re-segments and never re-emits page
text: a model labels the units, groups them, assigns each one a destination
BY ID, and writes the parent summary; code validates every one of those
decisions and appends the result to an append-only ledger.

**Two passes plus a verify, each on its own knob.** The shape follows the
packaged ``page_recompile_*.md`` prompt trio and the proposer/verifier split
issue athenaeum#1717 established:

1. :func:`label_units` -- a cheap pass on the ``classify`` knob, run in
   chunks, memoized by UNIT HASH in the ledger. A second plan over an
   unchanged page makes zero labelling calls.
2. :func:`propose_plan` -- grouping, routing and the parent summary on the
   new ``page_recompile`` knob, whose model defaults to whatever the
   ``verify`` knob resolves to (:func:`_get_page_recompile_model`).
3. :func:`verify_summary` -- a check on the ``verify`` knob that each
   summary sentence is supported by the units it cites. A failed verify
   marks the plan invalid; it never rewrites the summary.

Every call records its tokens under the knob that made it
(:meth:`athenaeum.models.TokenUsage.add`'s ``knob=``), so
``athenaeum spend --by-knob`` attributes a recompile plan correctly.

**Page text reaches a model only through**
:func:`athenaeum.prompt_safety.fence_untrusted`. There is no second fencing
path in this module and no f-string that interpolates a unit body directly
into a prompt.

**What code decides, not the model.** Four judgements are refused to the
model outright, because a model assertion is not evidence:

* ``duplicate:<unit id>`` is accepted only when :func:`normalise_unit_text`
  makes the two units' texts EQUAL (:func:`validate_plan`). A
  model-asserted duplicate over differing text invalidates the plan.
* The shortlist of existing pages offered for ``move`` and ``attach``
  (:func:`build_page_shortlist`) is built from name, alias and search
  lookup over the labels, with NO model call, and a uid outside it is a
  missing target.
* Whether a plan is ``structure-only`` or ``cross-page``
  (:func:`classify_plan_scope`) is read off the dispositions in code.
* Whether every summary sentence is CITED at all
  (:func:`check_summary_citations`) is pure text arithmetic over
  :mod:`athenaeum.footnote_markers`; only whether a cited sentence is
  SUPPORTED is asked of a model.

**Eligibility is S1's call, not a second copy.** :func:`plan_page` decides
whether the page it was handed is one this pass may act on at all by calling
:func:`athenaeum.page_recompile.classify_page_text` -- the predicates issue
athenaeum#2536 extracted out of ``find_recompile_candidates``'s loop body for
exactly this reason. Without it, reaching a page by uid through
:class:`~athenaeum.models.EntityIndex` would plan tombstoned, retired and
superseded pages, which the S1 scan exists to exclude.

Layering: L4 domain/pipeline, the same layer as its S1 sibling
:mod:`athenaeum.page_recompile` (same-layer imports are permitted by
``tests/test_layer_boundary.py``). Everything else it imports is below it:
:mod:`athenaeum.provider`, :mod:`athenaeum.prompt_safety` and
:mod:`athenaeum.page_relations` (all L3),
:mod:`athenaeum.config` (L2), :mod:`athenaeum.models`,
:mod:`athenaeum.footnote_markers` and :mod:`athenaeum.store` (L1),
:mod:`athenaeum.json_utils` and :mod:`athenaeum._retry` (L0).
"""

from __future__ import annotations

import hashlib
import importlib.resources
import json
import logging
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from athenaeum._retry import with_retry
from athenaeum.config import (
    DEFAULT_CLASSIFY_MODEL,
    DEFAULT_VERIFY_MODEL,
    resolve_model,
    resolve_oversize_hard_ceiling_chars,
    resolve_page_size_threshold_chars,
)
from athenaeum.footnote_markers import INLINE_MARKER_RE, parse_footnote_definitions
from athenaeum.json_utils import extract_json_object
from athenaeum.models import EntityIndex, TokenUsage, cache_usage_counts
from athenaeum.page_recompile import SegmentResult, Unit, check_conservation
from athenaeum.prompt_safety import fence_untrusted
from athenaeum.provider import resolve_max_tokens, resolve_thinking, response_text
from athenaeum.store import append_line_durable, now_iso

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Prompts (AC3). Text lives in packaged .md files, never as a literal here --
# ``policies/prompt-text-is-content.md``, the same convention
# ``pii_attribution.CONTACT_ATTRIBUTION_JUDGE_SYSTEM`` established.
# ---------------------------------------------------------------------------


def _load_prompt(filename: str) -> str:
    """Read one packaged prompt from ``athenaeum/prompts/`` (AC3)."""
    resource = importlib.resources.files("athenaeum.prompts").joinpath(filename)
    return resource.read_text(encoding="utf-8")


#: Module-level constants so ``prompt_registry._resolve`` can ``getattr``
#: them, even though the TEXT lives in the packaged ``.md`` files above.
PAGE_RECOMPILE_LABEL_SYSTEM: str = _load_prompt("page_recompile_label.md")
PAGE_RECOMPILE_PLAN_SYSTEM: str = _load_prompt("page_recompile_plan.md")
PAGE_RECOMPILE_VERIFY_SYSTEM: str = _load_prompt("page_recompile_verify.md")


# ---------------------------------------------------------------------------
# Knobs (AC2) and budgets.
# ---------------------------------------------------------------------------

#: The labelling pass rides the EXISTING ``classify`` knob (AC1) -- a
#: Haiku-class read-and-tag sibling of the Tier-2 classifier, deliberately
#: not a knob of its own.
LABEL_KNOB = "classify"

#: Grouping, routing and the parent summary (AC2): a new, independently
#: routed knob. "Independently routed" in the sense
#: ``librarian._LIBRARIAN_INDEPENDENTLY_ROUTED_KNOBS`` means -- this pass is
#: an operator-invoked CLI command, never a phase of the nightly
#: entity/merge pipeline, so it resolves its own client rather than riding
#: that pipeline's per-knob client cache.
PLAN_KNOB = "page_recompile"

#: The summary check (AC9). ``config.DEFAULT_VERIFY_MODEL`` has named this
#: knob ``verify`` since issue athenaeum#1717's proposer/verifier split;
#: this module is its first registered prompt.
VERIFY_KNOB = "verify"

#: Units per labelling call (AC1: "runs in chunks"). Sized so a page at the
#: 40,000-char hard ceiling, cut at the 4,000-char unit cap, is a handful of
#: calls rather than one unbounded one.
LABEL_CHUNK_UNITS = 8

#: Output budgets. The label pass emits one small object per unit in the
#: chunk; the plan pass emits the groups, one assignment per unit, any
#: merged text and the summary; the verify pass emits one verdict per
#: sentence.
_LABEL_MAX_TOKENS = 2048
_PLAN_MAX_TOKENS = 8192
_VERIFY_MAX_TOKENS = 2048

#: Input windows for the fenced page text. Both are bounds on what reaches
#: the model, applied by :func:`athenaeum.prompt_safety.fence_untrusted`.
_UNIT_FENCE_MAX_CHARS = 60_000
_SUMMARY_FENCE_MAX_CHARS = 8_000

#: Fence tags. One tag per distinct untrusted input, so the data-only clause
#: in each prompt can name exactly what it covers.
_UNITS_TAG = "page_units"
_LABELS_TAG = "page_labels"
_META_TAG = "page_meta"
_SHORTLIST_TAG = "page_shortlist"
_DEFINITIONS_TAG = "page_summaries"
_SUMMARY_TAG = "page_summary"


def _get_verify_model(config: dict[str, Any] | None = None) -> str:
    """The model the ``verify`` knob resolves to: env > yaml > code default.

    ``ATHENAEUM_VERIFY_MODEL`` > ``models.verify`` >
    :data:`athenaeum.config.DEFAULT_VERIFY_MODEL`. Factored out rather than
    inlined because :func:`_get_page_recompile_model` DEFAULTS to it (AC2),
    so the two cannot drift.
    """
    return resolve_model("verify", "ATHENAEUM_VERIFY_MODEL", DEFAULT_VERIFY_MODEL, config)


def _get_page_recompile_model(config: dict[str, Any] | None = None) -> str:
    """The model the ``page_recompile`` knob resolves to (AC2).

    ``ATHENAEUM_PAGE_RECOMPILE_MODEL`` > ``models.page_recompile`` > **the
    ``verify`` knob's resolved model**. Note the default is the verify
    knob's RESOLVED value, not its code default: an operator who moves
    ``models.verify`` moves this knob with it unless they pin
    ``models.page_recompile`` explicitly, which is what "defaults to the
    ``verify`` knob's model" has to mean to be useful.
    """
    return resolve_model(
        PLAN_KNOB, "ATHENAEUM_PAGE_RECOMPILE_MODEL", _get_verify_model(config), config
    )


def _get_label_model(config: dict[str, Any] | None = None) -> str:
    """The model the labelling pass uses -- the shared ``classify`` knob (AC1)."""
    return resolve_model(
        LABEL_KNOB, "ATHENAEUM_CLASSIFY_MODEL", DEFAULT_CLASSIFY_MODEL, config
    )


# ---------------------------------------------------------------------------
# The plan schema (AC4).
# ---------------------------------------------------------------------------

#: Per-unit dispositions that take no argument.
BARE_DISPOSITIONS: frozenset[str] = frozenset({"parent"})

#: Per-unit dispositions written ``<verb>:<argument>``. The argument's
#: meaning differs per verb and each is validated separately in
#: :func:`validate_plan`: a GROUP key for ``child``/``standalone``/``merge``,
#: an existing page UID for ``move``, a UNIT ID for ``duplicate``.
ARG_DISPOSITIONS: frozenset[str] = frozenset(
    {"child", "move", "standalone", "duplicate", "merge"}
)

#: Page-level actions (AC4), both ``<verb>:<existing uid>``.
PAGE_ACTIONS: frozenset[str] = frozenset({"attach", "attach-self-under"})

#: AC10's structure-only verbs: a plan built only from these touches no
#: other page's content and names no other page's uid.
STRUCTURE_ONLY_VERBS: frozenset[str] = frozenset(
    {"parent", "child", "duplicate", "merge"}
)

#: AC10's two scope values.
SCOPE_STRUCTURE_ONLY = "structure-only"
SCOPE_CROSS_PAGE = "cross-page"

#: :class:`PlanIssue` code for a verify that was MADE and came back with no
#: usable verdicts (AC9). Distinct from ``unsupported-sentence`` (the model
#: judged a sentence and rejected it) and from ``unverified-sentence`` (the
#: model answered, but said nothing about one sentence): this one means the
#: call itself yielded nothing to read. It is an issue, not merely a flag,
#: so :attr:`RecompilePlan.valid` rejects the plan -- the same "silence
#: reads as a failure, not a pass" rule the per-sentence check already
#: applies, applied one level up at the call.
VERIFY_UNUSABLE_CODE = "verify-unusable"


@dataclass(frozen=True)
class UnitLabel:
    """One unit's label (AC1), memoized by :attr:`unit_hash`."""

    unit_id: str
    unit_hash: str
    topic: str
    belongs: str
    about: str
    kind: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "unit": self.unit_id,
            "unit_hash": self.unit_hash,
            "topic": self.topic,
            "belongs": self.belongs,
            "about": self.about,
            "kind": self.kind,
        }


@dataclass(frozen=True)
class PlanGroup:
    """One proposed group (AC4): a name, a one-line description, a confidence."""

    key: str
    name: str
    description: str
    confidence: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "name": self.name,
            "description": self.description,
            "confidence": self.confidence,
        }


@dataclass(frozen=True)
class MergedSection:
    """One ``merge:<group>`` outcome (AC7).

    :attr:`unit_ids` is what the planner RECORDS about which units feed this
    merged section -- derived in code from the dispositions, never taken
    from the model's own say-so. Validation of :attr:`text` itself is the
    applier slice's job, a sibling of this issue under athenaeum#2525.
    """

    group: str
    text: str
    unit_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"group": self.group, "text": self.text, "units": list(self.unit_ids)}


@dataclass(frozen=True)
class PlanIssue:
    """One reason a plan is invalid. ``code`` is stable and greppable;
    ``detail`` names the offending id."""

    code: str
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "detail": self.detail}


@dataclass(frozen=True)
class PageCandidate:
    """One existing page offered to the model for ``move``/``attach``."""

    uid: str
    name: str
    matched: str  # the label text that found it

    def to_dict(self) -> dict[str, Any]:
        return {"uid": self.uid, "name": self.name, "matched": self.matched}


@dataclass(frozen=True)
class RecompilePlan:
    """A validated recompile plan -- the shadow record this slice produces."""

    uid: str
    content_hash: str
    dispositions: tuple[tuple[str, str], ...]  # (unit_id, disposition), in page order
    groups: tuple[PlanGroup, ...]
    merges: tuple[MergedSection, ...]
    page_actions: tuple[str, ...]
    summary: str
    scope: str
    issues: tuple[PlanIssue, ...]
    #: ``True`` every summary sentence was confirmed, ``False`` at least one
    #: was not, ``None`` no verdicts were obtained at all. ``None`` alone
    #: does NOT say whether a verify was tried -- read
    #: :attr:`verify_attempted` for that, and see its comment for why the
    #: two are separate fields.
    verified: bool | None
    #: Whether a verify call was actually MADE (AC9). ``False`` means the
    #: check was never requested: no client was supplied, or the caller
    #: passed ``verify=False``, or there was no summary to check. ``True``
    #: with ``verified is None`` means it was requested and came back
    #: unusable -- a provider error, or an answer carrying no verdicts --
    #: which :func:`verify_summary` also records as a
    #: :data:`VERIFY_UNUSABLE_CODE` issue so :attr:`valid` fails closed.
    #:
    #: Two fields rather than one four-valued one because they answer two
    #: different questions for two different readers: the ISSUE is what
    #: makes ``valid`` fail closed (the applier slice's gate), while this
    #: flag is what makes the ledger record self-describing without anyone
    #: having to infer "was it tried?" from the presence of an issue code.
    verify_attempted: bool = False

    @property
    def valid(self) -> bool:
        """No issue of any kind AND no sentence left unconfirmed (AC9).

        Fails CLOSED on an attempted-but-unusable verify: that path adds a
        :data:`VERIFY_UNUSABLE_CODE` issue, so the ``not self.issues`` term
        below rejects it. Only a verify that was never REQUESTED
        (:attr:`verify_attempted` ``False``) leaves a plan valid with
        :attr:`verified` ``None`` -- the caller chose not to check, which is
        a different thing from a check that failed to produce an answer.
        """
        return not self.issues and self.verified is not False

    def plan_id(self) -> str:
        """Deterministic id over the page identity and every decision in the plan.

        Keyed on the CONTENT HASH as well as the uid (AC11's pin): the same
        page re-planned after an edit is a different plan, and the same plan
        proposed twice over unchanged bytes is the same id.
        """
        payload = json.dumps(
            {
                "uid": self.uid,
                "content_hash": self.content_hash,
                "dispositions": [list(pair) for pair in self.dispositions],
                "groups": [g.to_dict() for g in self.groups],
                "merges": [m.to_dict() for m in self.merges],
                "page_actions": list(self.page_actions),
                "summary": self.summary,
            },
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def to_dict(self) -> dict[str, Any]:
        return {
            "uid": self.uid,
            "content_hash": self.content_hash,
            "dispositions": [{"unit": u, "disposition": d} for u, d in self.dispositions],
            "groups": [g.to_dict() for g in self.groups],
            "merges": [m.to_dict() for m in self.merges],
            "page_actions": list(self.page_actions),
            "summary": self.summary,
            "scope": self.scope,
            "issues": [i.to_dict() for i in self.issues],
            "verified": self.verified,
            "verify_attempted": self.verify_attempted,
            "valid": self.valid,
        }


def split_disposition(disposition: str) -> tuple[str, str]:
    """``"child:g1"`` -> ``("child", "g1")``; ``"parent"`` -> ``("parent", "")``.

    Splits on the FIRST colon only, so a uid or unit id containing one
    survives intact.
    """
    verb, sep, argument = disposition.partition(":")
    return (verb, argument) if sep else (verb, "")


# ---------------------------------------------------------------------------
# AC6: normalised-text equality, the code check behind ``duplicate``.
# ---------------------------------------------------------------------------

_WHITESPACE_RUN_RE = re.compile(r"\s+")


def normalise_unit_text(text: str) -> str:
    """The normal form two units must share for ``duplicate`` to be accepted (AC6).

    Whitespace runs collapse to one space, the result is stripped, and case
    is folded. Deliberately conservative: it forgives re-wrapping and
    capitalisation, which is what a genuine copy-paste duplicate differs by,
    and forgives nothing else -- a model that asserts two differently-worded
    units are duplicates is rejected rather than trusted.
    """
    return _WHITESPACE_RUN_RE.sub(" ", text).strip().casefold()


# ---------------------------------------------------------------------------
# AC8: summary citation check (pure text, no model).
# ---------------------------------------------------------------------------


def split_summary_sentences(summary: str) -> tuple[str, ...]:
    """Cut *summary* into sentences, keeping trailing footnote markers attached.

    A sentence ends at a run of ``.``/``!``/``?`` plus any footnote markers
    that immediately follow it, so both ``Claim.[^a]`` and ``Claim[^a].``
    keep their citation. A trailing fragment with no terminator is returned
    as a sentence too -- otherwise an uncited final clause would escape the
    AC8 check by simply omitting its full stop.

    Known limit, accepted: an abbreviation ("e.g.") splits. A split
    abbreviation can only ever make the citation check STRICTER (both halves
    must be cited), never laxer, so it cannot let an uncited sentence
    through.
    """
    text = summary.strip()
    if not text:
        return ()
    sentences: list[str] = []
    start = 0
    index = 0
    length = len(text)
    while index < length:
        if text[index] not in ".!?":
            index += 1
            continue
        end = index + 1
        while end < length and text[end] in ".!?":
            end += 1
        while True:
            cursor = end
            while cursor < length and text[cursor] in " \t":
                cursor += 1
            marker = INLINE_MARKER_RE.match(text, cursor)
            if marker is None:
                break
            end = marker.end()
        piece = text[start:end].strip()
        if piece:
            sentences.append(piece)
        start = end
        index = end
    tail = text[start:].strip()
    if tail:
        sentences.append(tail)
    return tuple(sentences)


def cited_labels(sentence: str) -> tuple[str, ...]:
    """Footnote labels cited by *sentence*, in order of first appearance."""
    return tuple(dict.fromkeys(m.group(1) for m in INLINE_MARKER_RE.finditer(sentence)))


def summary_definition_labels(units: Sequence[Unit], body: str) -> frozenset[str]:
    """The labels a summary sentence may legally cite (AC8).

    A marker must resolve to "a definition from the summarised units": the
    label has to be CITED by at least one of the units being summarised AND
    DEFINED on the page. A label defined on the page but cited by no
    summarised unit is not a source for this summary; a label a unit cites
    but the page never defines is dangling and equally unusable.
    """
    defined = set(parse_footnote_definitions(body))
    cited = {label for unit in units for label in unit.footnote_labels}
    return frozenset(cited & defined)


def check_summary_citations(
    summary: str, units: Sequence[Unit], body: str
) -> tuple[PlanIssue, ...]:
    """Every summary sentence carries a resolving footnote marker (AC8).

    Two failures, each its own code: ``uncited-sentence`` (no marker at all)
    and ``unresolved-citation`` (a marker that is not one of
    :func:`summary_definition_labels`). An EMPTY summary is itself an issue
    -- a page that keeps its identity needs something to say about itself.
    """
    allowed = summary_definition_labels(units, body)
    sentences = split_summary_sentences(summary)
    if not sentences:
        return (PlanIssue("empty-summary", ""),)
    issues: list[PlanIssue] = []
    for sentence in sentences:
        labels = cited_labels(sentence)
        if not labels:
            issues.append(PlanIssue("uncited-sentence", sentence))
            continue
        for label in labels:
            if label not in allowed:
                issues.append(PlanIssue("unresolved-citation", label))
    return tuple(issues)


# ---------------------------------------------------------------------------
# AC10: scope classification (code, not the model).
# ---------------------------------------------------------------------------


def classify_plan_scope(
    dispositions: Iterable[tuple[str, str]], page_actions: Iterable[str]
) -> str:
    """:data:`SCOPE_STRUCTURE_ONLY` or :data:`SCOPE_CROSS_PAGE` (AC10).

    Structure-only means every per-unit disposition's verb is in
    :data:`STRUCTURE_ONLY_VERBS` and there is no page-level action at all.
    ``move`` and ``standalone`` are cross-page (one names another page's
    uid, the other mints a page outside this parent), and ``attach`` /
    ``attach-self-under`` are cross-page by construction. An ALIAS MOVE
    (:data:`ALIAS_MOVE_VERB`, issue athenaeum#2548 AC8) is a page action
    like any other, so it is covered by the first clause with no extra
    case: "any page action at all" is already the rule, and that is why
    the alias-move grammar was given the page-action shape rather than a
    field of its own.
    """
    if any(True for _ in page_actions):
        return SCOPE_CROSS_PAGE
    for _unit_id, disposition in dispositions:
        verb, _argument = split_disposition(disposition)
        if verb not in STRUCTURE_ONLY_VERBS:
            return SCOPE_CROSS_PAGE
    return SCOPE_STRUCTURE_ONLY


# ---------------------------------------------------------------------------
# Cross-page actions (issue athenaeum#2548, slice S7).
# ---------------------------------------------------------------------------

#: The alias-move action's verb, written ``move-alias:<target uid>:<alias>``
#: in :attr:`RecompilePlan.page_actions` (issue athenaeum#2548 AC8/AC12).
#: A uid is 8 hex characters (:func:`athenaeum.models.generate_uid`) and so
#: never contains a colon, which is what makes
#: :func:`split_disposition`-then-partition unambiguous for an alias that
#: does.
#:
#: **Deliberately NOT a member of** :data:`PAGE_ACTIONS`. That frozenset is
#: the MODEL-facing vocabulary ``validate_plan`` accepts, and the packaged
#: ``page_recompile_plan.md`` prompt does not teach this action -- so a
#: model cannot propose an alias move, and a plan drafted by this pass
#: never carries one. AC12's default ("aliases stay on the parent") is
#: therefore the only behaviour the nightly pass can produce; the grammar
#: exists so an operator-authored or later-slice plan record CAN list one,
#: and so :func:`classify_plan_scope` classes such a plan cross-page
#: (AC8) and :mod:`athenaeum.page_recompile_apply` carries it out under an
#: approved decision item like every other cross-page action.
ALIAS_MOVE_VERB = "move-alias"


def parse_alias_move(action: str) -> tuple[str, str] | None:
    """``"move-alias:<uid>:<alias>"`` -> ``(uid, alias)``, else ``None``.

    ``None`` for any other verb and for a malformed argument (no second
    colon, an empty uid or an empty alias) -- a caller treats that as "not
    an alias move" and its own page-action validation reports it.
    """
    verb, argument = split_disposition(action)
    if verb != ALIAS_MOVE_VERB:
        return None
    uid, sep, alias = argument.partition(":")
    if not sep:
        return None
    uid = uid.strip()
    alias = alias.strip()
    if not uid or not alias:
        return None
    return uid, alias


#: :class:`PlanIssue` codes :func:`resolve_standalone_groups` can add.
STANDALONE_NAME_IS_SOURCE_CODE = "standalone-name-is-the-source-page"
STANDALONE_NAME_INACTIVE_CODE = "standalone-name-resolves-to-inactive-page"
STANDALONE_NAME_NO_UID_CODE = "standalone-name-resolves-to-a-page-with-no-uid"


def resolve_standalone_groups(plan: RecompilePlan, index: EntityIndex) -> RecompilePlan:
    """Resolve every ``standalone:<group>``'s PROPOSED NAME against existing
    names and aliases, converting a hit into a ``move`` (issue
    athenaeum#2548 AC5).

    The AC's own wording: "The proposed name is resolved against existing
    names and aliases first; a hit converts the action to a move."
    :meth:`athenaeum.models.EntityIndex.lookup` is keyed by name AND by
    every alias, so one call covers both -- the same reuse
    :func:`build_page_shortlist` already relies on.

    Run HERE, in the planner, rather than at apply time, for two reasons.
    The recorded plan (and therefore the decision item an operator answers,
    and the plan sibling slice athenaeum#2549 reads) then states the action
    that will actually be taken; and an operator who approved "create a new
    page" must never have that silently become "write onto an existing
    page" after the fact -- :func:`athenaeum.page_recompile_apply.compute_apply`
    REFUSES a standalone whose name already resolves, rather than
    converting it a second time.

    Two resolutions are refused rather than converted, each with its own
    issue code so the refusal is greppable:

    * the name resolves to the page being planned
      (:data:`STANDALONE_NAME_IS_SOURCE_CODE`) -- a page cannot be the
      target of its own move, and minting a second page under the parent's
      own name would make the index ambiguous;
    * the name resolves to a page carrying no ``uid:``
      (:data:`STANDALONE_NAME_NO_UID_CODE`) -- such a page cannot be a move
      target, and a second live page under a name the index already
      resolves would make its name lookup ambiguous. See the call site for
      why :meth:`athenaeum.models.EntityIndex.get_by_uid`, not
      ``entry.uid``'s truthiness, is the discriminator;
    * the name resolves to an INACTIVE page
      (:data:`STANDALONE_NAME_INACTIVE_CODE`, tombstoned / retired /
      superseded per :func:`athenaeum.page_relations.is_inactive_relation_target`,
      the same composite predicate the candidate scan and the relation
      writer already use) -- moving content onto a page the corpus has
      retired would bury it, and creating a live page under a retired
      page's name would resurrect a name deliberately taken out of use.
    """
    from athenaeum.models import parse_frontmatter
    from athenaeum.page_relations import is_inactive_relation_target

    groups_by_key = {group.key: group for group in plan.groups}
    wanted = {
        argument
        for _unit_id, disposition in plan.dispositions
        for verb, argument in (split_disposition(disposition),)
        if verb == "standalone" and argument
    }
    if not wanted:
        return plan

    converted: dict[str, str] = {}
    issues = list(plan.issues)
    for group_key in sorted(wanted):
        group = groups_by_key.get(group_key)
        name = (group.name if group is not None else "").strip()
        if not name:
            continue
        entry = index.lookup(name)
        if entry is None:
            continue
        if entry.uid == plan.uid:
            issues.append(PlanIssue(STANDALONE_NAME_IS_SOURCE_CODE, group_key))
            continue
        # :attr:`athenaeum.models.IndexEntry.uid` is ``uid or name`` (see
        # ``EntityIndex._apply_record``), so for a page that carries a
        # ``name:`` but NO ``uid:`` it holds the page's NAME. Such an entry
        # is therefore not a usable move target: `get_by_uid` indexes only
        # pages that really have a uid, so converting here would mint
        # ``move:<name>``, pass validation (which already ran, and whose
        # `known_uids` this conversion is not re-checked against), be
        # queued as a valid plan -- and then be refused at apply time with
        # `move-target-missing`.
        #
        # `get_by_uid` is the discriminator, and it is the SAME lookup the
        # applier resolves a move target with, so plan time and apply time
        # cannot disagree. A uid-less hit is REFUSED rather than left
        # standalone: minting a second live page under a name the index
        # already resolves would make `_by_name` ambiguous (it is
        # last-write-wins), which is a worse outcome than declining and
        # saying why. Giving the corpus page a uid makes the name a real
        # move target on the next pass.
        path = index.get_by_uid(entry.uid)
        if path is None:
            issues.append(PlanIssue(STANDALONE_NAME_NO_UID_CODE, group_key))
            continue
        meta: dict[str, Any] = {}
        try:
            parsed, _body = parse_frontmatter(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            parsed = {}
        if isinstance(parsed, dict):
            meta = parsed
        if is_inactive_relation_target(meta):
            issues.append(PlanIssue(STANDALONE_NAME_INACTIVE_CODE, group_key))
            continue
        converted[group_key] = entry.uid

    if not converted and len(issues) == len(plan.issues):
        return plan

    # A plain loop, never a comprehension with a walrus: a walrus inside a
    # comprehension binds in the ENCLOSING scope, the exact defect
    # ``page_recompile_undo.compute_undo``'s own comment records.
    rewritten: list[tuple[str, str]] = []
    for unit_id, disposition in plan.dispositions:
        verb, argument = split_disposition(disposition)
        if verb == "standalone" and argument in converted:
            rewritten.append((unit_id, f"move:{converted[argument]}"))
        else:
            rewritten.append((unit_id, disposition))
    dispositions = tuple(rewritten)
    return replace(
        plan,
        dispositions=dispositions,
        issues=tuple(issues),
        scope=classify_plan_scope(dispositions, plan.page_actions),
    )


# ---------------------------------------------------------------------------
# AC5/AC6/AC7: validation.
# ---------------------------------------------------------------------------


def _coerce_confidence(raw: Any) -> float | None:
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if not 0.0 <= value <= 1.0:
        return None
    return value


def _parse_groups(raw_groups: Any) -> tuple[tuple[PlanGroup, ...], tuple[PlanIssue, ...]]:
    """Parse the model's ``groups`` array; every group needs all four fields (AC4)."""
    groups: list[PlanGroup] = []
    issues: list[PlanIssue] = []
    if raw_groups is None:
        return (), ()
    if not isinstance(raw_groups, list):
        return (), (PlanIssue("malformed-groups", ""),)
    seen: set[str] = set()
    for entry in raw_groups:
        if not isinstance(entry, dict):
            issues.append(PlanIssue("malformed-groups", ""))
            continue
        key = str(entry.get("key") or "").strip()
        name = str(entry.get("name") or "").strip()
        description = str(entry.get("description") or "").strip()
        confidence = _coerce_confidence(entry.get("confidence"))
        if not key:
            issues.append(PlanIssue("group-missing-key", ""))
            continue
        if key in seen:
            issues.append(PlanIssue("duplicate-group-key", key))
            continue
        seen.add(key)
        if not name:
            issues.append(PlanIssue("group-missing-name", key))
        if not description:
            issues.append(PlanIssue("group-missing-description", key))
        elif "\n" in description:
            # AC4: "a one-line description". A multi-line blob is a section,
            # not a description, and would render as one in any listing.
            issues.append(PlanIssue("group-description-not-one-line", key))
        if confidence is None:
            issues.append(PlanIssue("group-missing-confidence", key))
        groups.append(
            PlanGroup(
                key=key,
                name=name,
                description=description,
                confidence=confidence if confidence is not None else 0.0,
            )
        )
    return tuple(groups), tuple(issues)


def _parse_assignments(raw: Any) -> tuple[tuple[tuple[str, str], ...], tuple[PlanIssue, ...]]:
    """Parse ``assignments`` as an ORDERED LIST, not a mapping.

    The wire shape is deliberately a list of ``{"unit": ..., "disposition":
    ...}`` objects: a JSON object keyed by unit id could not express "assigns
    a unit twice" at all (the second key would silently win), and that case
    is one AC5 requires be REJECTED rather than quietly resolved.
    """
    if not isinstance(raw, list):
        return (), (PlanIssue("malformed-assignments", ""),)
    pairs: list[tuple[str, str]] = []
    issues: list[PlanIssue] = []
    for entry in raw:
        if not isinstance(entry, dict):
            issues.append(PlanIssue("malformed-assignments", ""))
            continue
        unit_id = str(entry.get("unit") or "").strip()
        disposition = str(entry.get("disposition") or "").strip()
        if not unit_id or not disposition:
            issues.append(PlanIssue("malformed-assignments", unit_id))
            continue
        pairs.append((unit_id, disposition))
    return tuple(pairs), tuple(issues)


def _parse_page_actions(raw: Any) -> tuple[tuple[str, ...], tuple[PlanIssue, ...]]:
    if raw is None:
        return (), ()
    if not isinstance(raw, list):
        return (), (PlanIssue("malformed-page-actions", ""),)
    actions: list[str] = []
    issues: list[PlanIssue] = []
    for entry in raw:
        if not isinstance(entry, str) or not entry.strip():
            issues.append(PlanIssue("malformed-page-actions", ""))
            continue
        actions.append(entry.strip())
    return tuple(actions), tuple(issues)


def _parse_merges(raw: Any) -> tuple[dict[str, str], tuple[PlanIssue, ...]]:
    if raw is None:
        return {}, ()
    if not isinstance(raw, list):
        return {}, (PlanIssue("malformed-merges", ""),)
    texts: dict[str, str] = {}
    issues: list[PlanIssue] = []
    for entry in raw:
        if not isinstance(entry, dict):
            issues.append(PlanIssue("malformed-merges", ""))
            continue
        group = str(entry.get("group") or "").strip()
        text = entry.get("text")
        if not group:
            issues.append(PlanIssue("malformed-merges", ""))
            continue
        if group in texts:
            issues.append(PlanIssue("duplicate-merge-group", group))
            continue
        texts[group] = text if isinstance(text, str) else ""
    return texts, tuple(issues)


def validate_plan(
    payload: Mapping[str, Any],
    segment: SegmentResult,
    body: str,
    *,
    uid: str,
    content_hash: str,
    known_uids: Iterable[str] = (),
) -> RecompilePlan:
    """Turn a model payload into a :class:`RecompilePlan`, checked in code.

    Every rule this enforces is an acceptance criterion of issue
    athenaeum#2537, and every violation is recorded as a
    :class:`PlanIssue` rather than raised, so a caller can report ALL of
    what is wrong with a plan in one pass instead of the first thing:

    * AC5 -- ``unknown-unit`` (a disposition names a unit id the page does
      not have), ``missing-target`` (a ``move``/``attach``/
      ``attach-self-under`` uid that is not in *known_uids*),
      ``duplicate-assignment`` (a unit assigned twice), plus
      ``unassigned-unit`` for a unit the plan forgot, via
      :func:`athenaeum.page_recompile.check_conservation` -- the S1 checker,
      reused rather than re-implemented.
    * AC6 -- ``duplicate-text-mismatch`` unless
      :func:`normalise_unit_text` makes the two units equal.
    * AC7 -- ``merge-too-few-units`` / ``merge-missing-text``, and the
      recorded feeder units per :class:`MergedSection`.
    * AC8 -- the citation issues from :func:`check_summary_citations`.
    * AC4 -- ``unknown-disposition`` / ``unknown-group`` / the group-field
      issues from :func:`_parse_groups`.

    *known_uids* is the set of page uids a target may name. Passing the
    empty default means "no page target is resolvable", which makes every
    ``move``/``attach`` a missing target -- the safe posture for a caller
    that has no index, never a silent pass.
    """
    issues: list[PlanIssue] = []

    groups, group_issues = _parse_groups(payload.get("groups"))
    issues.extend(group_issues)
    assignments, assignment_issues = _parse_assignments(payload.get("assignments"))
    issues.extend(assignment_issues)
    page_actions, action_issues = _parse_page_actions(payload.get("page_actions"))
    issues.extend(action_issues)
    merge_texts, merge_issues = _parse_merges(payload.get("merges"))
    issues.extend(merge_issues)

    units_by_id = {unit.unit_id: unit for unit in segment.units}
    group_keys = {group.key for group in groups}
    target_uids = set(known_uids)

    # AC5: unknown unit ids, checked before conservation so an unknown id is
    # reported as such rather than only as a missing real one.
    for unit_id, _disposition in assignments:
        if unit_id not in units_by_id:
            issues.append(PlanIssue("unknown-unit", unit_id))

    # AC5: every known unit assigned exactly once. check_conservation is the
    # S1 (athenaeum#2534 AC7) checker; feeding it one single-element list per
    # assignment keyed by position makes "twice" and "never" both fall out of
    # its existing contract.
    assigned: dict[str, list[str]] = {}
    for position, (unit_id, _disposition) in enumerate(assignments):
        assigned[f"{position}"] = [unit_id]
    conservation = check_conservation([u.unit_id for u in segment.units], assigned)
    for unit_id in conservation.duplicated:
        issues.append(PlanIssue("duplicate-assignment", unit_id))
    for unit_id in conservation.missing:
        issues.append(PlanIssue("unassigned-unit", unit_id))

    merge_members: dict[str, list[str]] = {}
    for unit_id, disposition in assignments:
        verb, argument = split_disposition(disposition)
        if verb in BARE_DISPOSITIONS:
            if argument:
                issues.append(PlanIssue("unknown-disposition", disposition))
            continue
        if verb not in ARG_DISPOSITIONS:
            issues.append(PlanIssue("unknown-disposition", disposition))
            continue
        if not argument:
            issues.append(PlanIssue("unknown-disposition", disposition))
            continue
        if verb in {"child", "standalone", "merge"}:
            if argument not in group_keys:
                issues.append(PlanIssue("unknown-group", argument))
            if verb == "merge":
                merge_members.setdefault(argument, []).append(unit_id)
        elif verb == "move":
            if argument not in target_uids:
                issues.append(PlanIssue("missing-target", argument))
        elif verb == "duplicate":
            # AC6: a model ASSERTION of duplication is not evidence. The
            # two texts have to be equal under normalisation or the plan is
            # rejected outright.
            other = units_by_id.get(argument)
            if other is None:
                issues.append(PlanIssue("unknown-unit", argument))
            elif argument == unit_id:
                issues.append(PlanIssue("duplicate-self-reference", unit_id))
            else:
                this = units_by_id.get(unit_id)
                if this is not None and normalise_unit_text(this.text) != normalise_unit_text(
                    other.text
                ):
                    issues.append(PlanIssue("duplicate-text-mismatch", unit_id))

    # AC4/AC5: page-level actions.
    for action in page_actions:
        verb, argument = split_disposition(action)
        if verb not in PAGE_ACTIONS or not argument:
            issues.append(PlanIssue("unknown-page-action", action))
            continue
        if argument not in target_uids:
            issues.append(PlanIssue("missing-target", argument))

    # AC7: a merge names two or more units and carries the merged text; the
    # feeder units are RECORDED here, derived from the dispositions.
    merges: list[MergedSection] = []
    for group_key in sorted(merge_members):
        members = tuple(merge_members[group_key])
        if len(members) < 2:
            issues.append(PlanIssue("merge-too-few-units", group_key))
        text = merge_texts.get(group_key, "")
        if not text.strip():
            issues.append(PlanIssue("merge-missing-text", group_key))
        merges.append(MergedSection(group=group_key, text=text, unit_ids=members))
    for group_key in sorted(set(merge_texts) - set(merge_members)):
        issues.append(PlanIssue("merge-text-without-units", group_key))

    summary = payload.get("summary")
    summary_text = summary if isinstance(summary, str) else ""
    issues.extend(check_summary_citations(summary_text, segment.units, body))

    return RecompilePlan(
        uid=uid,
        content_hash=content_hash,
        dispositions=assignments,
        groups=groups,
        merges=tuple(merges),
        page_actions=page_actions,
        summary=summary_text,
        scope=classify_plan_scope(assignments, page_actions),
        issues=tuple(issues),
        verified=None,
    )


# ---------------------------------------------------------------------------
# The shortlist (Plan: "name, alias and search lookup over the labels, with
# no model call").
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")


def _tokens(text: str) -> frozenset[str]:
    return frozenset(token.casefold() for token in _TOKEN_RE.findall(text) if len(token) > 2)


def build_page_shortlist(
    labels: Iterable[UnitLabel],
    index: EntityIndex,
    *,
    self_uid: str = "",
    limit: int = 20,
) -> tuple[PageCandidate, ...]:
    """Existing pages a ``move``/``attach`` may name, found WITHOUT a model call.

    Two deterministic lookups over the labels' ``about``/``topic`` strings,
    in this order:

    1. **Name and alias.** :meth:`athenaeum.models.EntityIndex.lookup` is
       already keyed by name AND by every alias, so one call covers both.
    2. **Search.** A token-overlap scan of the same index's keys, which is
       the search this module can run with no backend, no cache and no
       network -- a shortlist is a set of candidates for a model to choose
       among, not a ranked answer, so recall matters and precision does not.

    The page being planned is excluded by *self_uid*: a page cannot be the
    target of its own ``move``. Results are deterministic (sorted by uid)
    and capped at *limit*.
    """
    wanted: list[str] = []
    for label in labels:
        for candidate_text in (label.about, label.topic):
            text = candidate_text.strip()
            if text and text not in wanted:
                wanted.append(text)

    found: dict[str, PageCandidate] = {}

    def _record(uid: str, name: str, matched: str) -> None:
        if not uid or uid == self_uid or uid in found:
            return
        found[uid] = PageCandidate(uid=uid, name=name, matched=matched)

    for text in wanted:
        entry = index.lookup(text)
        if entry is not None:
            _record(entry.uid, text, text)

    index_keys = list(index)
    for text in wanted:
        tokens = _tokens(text)
        if not tokens:
            continue
        for key in index_keys:
            if not tokens & _tokens(key):
                continue
            entry = index.lookup(key)
            if entry is not None:
                _record(entry.uid, key, text)

    ordered = sorted(found.values(), key=lambda c: c.uid)
    return tuple(ordered[:limit])


# ---------------------------------------------------------------------------
# The ledger (AC11), append-only.
# ---------------------------------------------------------------------------

#: Schema version stamped on every ledger record.
PAGE_RECOMPILE_LEDGER_VERSION = 1

#: Sidecar filename under ``wiki_root`` (AC11).
PAGE_RECOMPILE_LEDGER_FILENAME = "_page_recompiles.jsonl"

#: Record kinds. ``label`` records are the AC1 memo (one per distinct unit
#: hash); ``plan`` records are the AC11 shadow record. Both live in one
#: append-only file, the pattern ``auto_apply_proposals.py`` already uses
#: for its proposal/approve/reject kinds.
LABEL_KIND = "label"
PLAN_KIND = "plan"

#: Record kind for an undone apply, WRITTEN by
#: :mod:`athenaeum.page_recompile_undo` (issue athenaeum#2541) and READ here
#: by :func:`undone_page_skip_reason`. It lives in this module, not in the
#: writer, precisely because of that read: this module owns the ledger, and
#: the planner has to recognise an ``undo`` record to honour it -- importing
#: the writer to learn the string would make the planner and the undo module
#: an import cycle (``tests/test_import_graph_acyclic.py`` allows none). That
#: is also why it is not a sibling of
#: :data:`athenaeum.page_recompile_apply.APPLY_KIND`, which no reader in this
#: module needs.
UNDO_KIND = "undo"


def default_page_recompile_ledger_path(wiki_root: Path) -> Path:
    """Default ledger path: ``<wiki_root>/_page_recompiles.jsonl``."""
    return Path(wiki_root) / PAGE_RECOMPILE_LEDGER_FILENAME


def read_page_recompile_ledger(
    wiki_root: Path, *, ledger_path: Path | None = None
) -> list[dict[str, Any]]:
    """Every well-formed ledger record, tolerating a torn trailing line.

    Same tolerant-reader contract as
    :func:`athenaeum.auto_apply_proposals.read_auto_apply_proposals_ledger`.
    """
    target = (
        ledger_path if ledger_path is not None else default_page_recompile_ledger_path(wiki_root)
    )
    if not target.exists():
        return []
    try:
        raw_text = target.read_text(encoding="utf-8")
    except OSError:
        return []
    records: list[dict[str, Any]] = []
    for line in raw_text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            record = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def _append_record(path: Path, record: Mapping[str, Any]) -> None:
    append_line_durable(path, (json.dumps(record, sort_keys=True) + "\n").encode("utf-8"))


def append_plan_record(
    plan: RecompilePlan,
    wiki_root: Path,
    *,
    ledger_path: Path | None = None,
    now: datetime | None = None,
) -> str:
    """Append *plan* as a shadow record and return its plan id (AC11).

    Writes exactly one line to the ledger and touches nothing else -- in
    particular, no page.
    """
    target = (
        ledger_path if ledger_path is not None else default_page_recompile_ledger_path(wiki_root)
    )
    plan_id = plan.plan_id()
    record: dict[str, Any] = {
        "v": PAGE_RECOMPILE_LEDGER_VERSION,
        "kind": PLAN_KIND,
        "id": plan_id,
        "ts": now_iso(now),
        "plan": plan.to_dict(),
    }
    _append_record(target, record)
    return plan_id


def find_plan_record(
    plan_id: str, wiki_root: Path, *, ledger_path: Path | None = None
) -> dict[str, Any] | None:
    """The LAST ``plan`` record carrying *plan_id* (AC11's ``recompile show``).

    Last, not first: the ledger is append-only, so a re-planned page's newer
    record is the current one.
    """
    match: dict[str, Any] | None = None
    for record in read_page_recompile_ledger(wiki_root, ledger_path=ledger_path):
        if record.get("kind") == PLAN_KIND and str(record.get("id")) == plan_id:
            match = record
    return match


def undone_page_skip_reason(
    wiki_root: Path,
    *,
    uid: str,
    body: str,
    ledger_path: Path | None = None,
) -> str:
    """Why *uid* must NOT be planned again yet, or ``""`` (issue
    athenaeum#2541 AC6).

    An operator who undoes an applied recompile has made a judgement about
    that page. Without this check the very next planning pass would read the
    restored page -- oversized again, exactly as it was before the apply --
    and draft the same plan, so an unattended pass would re-apply what was
    just undone. :mod:`athenaeum.page_recompile_undo` appends an
    :data:`UNDO_KIND` record naming each restored page and the pre-image
    content hash it restored; this function skips the page while its live
    body still hashes to that value.

    **A hash comparison, never a timestamp or a permanent block.** The
    moment the page's content differs from the restored pre-image -- someone
    edited it, or a later apply changed it again -- the hash stops matching
    and planning resumes on its own. "Not re-applied automatically" is
    exactly that: the undo is respected until the page genuinely moves, not
    forever, and the issue explicitly puts time-based expiry of undo out of
    scope, so nothing here expires.

    The LAST matching record wins, the same append-only rule
    :func:`find_plan_record` applies: a page that was undone, re-planned,
    re-applied and undone again is governed by its most recent undo.
    """
    current = page_content_hash(body)
    reason = ""
    for record in read_page_recompile_ledger(wiki_root, ledger_path=ledger_path):
        if record.get("kind") != UNDO_KIND:
            continue
        restored = record.get("restored")
        if not isinstance(restored, list):
            continue
        for row in restored:
            if not isinstance(row, dict) or row.get("uid") != uid:
                continue
            pre_hash = row.get("pre_content_hash")
            if isinstance(pre_hash, str) and pre_hash and pre_hash == current:
                reason = (
                    f"page was restored by an undo of recompile plan "
                    f"{record.get('plan_id') or record.get('id') or '(unknown)'} and has "
                    "not changed since; it is skipped until its content hash "
                    "differs from the restored pre-image"
                )
            else:
                # A later undo whose pre-image is NOT what the page holds now
                # clears an earlier skip rather than leaving it latched.
                reason = ""
    return reason


def read_label_cache(
    wiki_root: Path, *, ledger_path: Path | None = None
) -> dict[str, dict[str, Any]]:
    """Unit hash -> the last label recorded for it (AC1's memo)."""
    cache: dict[str, dict[str, Any]] = {}
    for record in read_page_recompile_ledger(wiki_root, ledger_path=ledger_path):
        if record.get("kind") != LABEL_KIND:
            continue
        label = record.get("label")
        if not isinstance(label, dict):
            continue
        unit_hash = str(label.get("unit_hash") or "")
        if unit_hash:
            cache[unit_hash] = label
    return cache


# ---------------------------------------------------------------------------
# AC1: the labelling pass.
# ---------------------------------------------------------------------------


def unit_hash(unit: Unit) -> str:
    """The content-addressed half of a unit id -- the AC1 cache key.

    :class:`athenaeum.page_recompile.Unit` ids are ``"{ordinal:04d}-{hash}"``
    where the hash changes when and only when the text does. Keying the memo
    on the HASH alone (not the whole id) is what makes a label survive the
    unit moving to a different ordinal on an unrelated edit elsewhere in the
    page.
    """
    _ordinal, _sep, digest = unit.unit_id.partition("-")
    return digest


def _render_units_for_prompt(units: Sequence[Unit]) -> str:
    """The units' bodies, fenced ONCE by :func:`fence_untrusted` (AC3)."""
    blocks = []
    for unit in units:
        heading = " > ".join(unit.heading_path) if unit.heading_path else "(no heading)"
        blocks.append(f"[{unit.unit_id}] {heading}\n{unit.text}")
    return fence_untrusted(
        "\n\n".join(blocks), tag=_UNITS_TAG, max_chars=_UNIT_FENCE_MAX_CHARS
    )


def _call_model(
    client: Any,
    *,
    model: str,
    system: str,
    user_message: str,
    max_tokens: int,
    max_tokens_env: str,
    thinking_env: str,
    budget_knob: str,
    usage_knob: str,
    config: dict[str, Any] | None,
    usage: TokenUsage | None,
    description: str,
) -> dict[str, Any] | None:
    """One model call, with retry, usage attribution (AC12) and JSON extraction.

    Fails SOFT: any transport error, malformed response or non-object JSON
    returns ``None`` and is logged, never raised. A shadow pass that cannot
    reach a model has nothing to say; it must not take down its caller.
    """
    try:
        response = with_retry(
            lambda: client.messages.create(
                model=model,
                max_tokens=resolve_max_tokens(budget_knob, max_tokens_env, max_tokens, config),
                thinking=cast(
                    "Any", resolve_thinking(budget_knob, thinking_env, "disabled", config)
                ),
                system=system,
                messages=cast("Any", [{"role": "user", "content": user_message}]),
            ),
            description=description,
        )
    except Exception as exc:  # noqa: BLE001 -- shadow pass, never raises (see docstring)
        log.warning("page_recompile_plan: %s call failed (%s)", description, exc)
        return None

    if usage is not None and hasattr(response, "usage"):
        input_toks, output_toks, cache_creation, cache_read = cache_usage_counts(response)
        # AC12: tokens land under the knob that made THIS call, not under a
        # single blended knob for the whole pass.
        usage.add(
            input_toks,
            output_toks,
            cache_creation,
            cache_read,
            model=model,
            knob=usage_knob,
        )

    try:
        raw_text = response_text(response)
    except (AttributeError, IndexError, ValueError):
        log.warning("page_recompile_plan: malformed %s response", description)
        return None
    payload = extract_json_object(raw_text)
    if not isinstance(payload, dict):
        log.warning("page_recompile_plan: no JSON object in %s response", description)
        return None
    return payload


def label_units(
    units: Sequence[Unit],
    client: Any,
    *,
    wiki_root: Path,
    config: dict[str, Any] | None = None,
    usage: TokenUsage | None = None,
    ledger_path: Path | None = None,
    chunk_size: int = LABEL_CHUNK_UNITS,
    record_cache: bool = True,
) -> dict[str, UnitLabel]:
    """Label every unit, in chunks, on the ``classify`` knob, memoized (AC1).

    The memo is keyed by :func:`unit_hash` and lives in the ledger, so a
    second plan over an UNCHANGED page makes no labelling call at all --
    which is exactly what the AC's stub-client call count asserts. Only the
    units whose hash is absent from the memo are sent, and they are sent
    *chunk_size* at a time.

    A unit the model declines to label (or a chunk whose call fails) simply
    has no entry in the result; callers treat an absent label as "unknown",
    never as an assertion.
    """
    cached = read_label_cache(wiki_root, ledger_path=ledger_path)
    labels: dict[str, UnitLabel] = {}
    pending: list[Unit] = []
    for unit in units:
        digest = unit_hash(unit)
        memo = cached.get(digest)
        if memo is not None:
            labels[unit.unit_id] = UnitLabel(
                unit_id=unit.unit_id,
                unit_hash=digest,
                topic=str(memo.get("topic") or ""),
                belongs=str(memo.get("belongs") or ""),
                about=str(memo.get("about") or ""),
                kind=str(memo.get("kind") or ""),
            )
        else:
            pending.append(unit)

    if not pending or client is None:
        return labels

    model = _get_label_model(config)
    target = (
        ledger_path if ledger_path is not None else default_page_recompile_ledger_path(wiki_root)
    )
    step = max(1, chunk_size)
    for start in range(0, len(pending), step):
        chunk = pending[start : start + step]
        payload = _call_model(
            client,
            model=model,
            system=PAGE_RECOMPILE_LABEL_SYSTEM,
            user_message=_render_units_for_prompt(chunk),
            max_tokens=_LABEL_MAX_TOKENS,
            max_tokens_env="ATHENAEUM_PAGE_RECOMPILE_LABEL_MAX_TOKENS",
            thinking_env="ATHENAEUM_PAGE_RECOMPILE_LABEL_THINKING",
            budget_knob="page_recompile_label",
            usage_knob=LABEL_KNOB,
            config=config,
            usage=usage,
            description="page_recompile_label",
        )
        if payload is None:
            continue
        by_id = {unit.unit_id: unit for unit in chunk}
        raw_labels = payload.get("labels")
        if not isinstance(raw_labels, list):
            log.warning("page_recompile_plan: label chunk returned no labels array")
            continue
        for entry in raw_labels:
            if not isinstance(entry, dict):
                continue
            unit_id = str(entry.get("unit") or "")
            labelled = by_id.get(unit_id)
            if labelled is None:
                # The model named a unit outside this chunk. Dropped, not
                # trusted: a label is only ever recorded against a unit
                # whose bytes were actually in the request.
                continue
            label = UnitLabel(
                unit_id=unit_id,
                unit_hash=unit_hash(labelled),
                topic=str(entry.get("topic") or ""),
                belongs=str(entry.get("belongs") or ""),
                about=str(entry.get("about") or ""),
                kind=str(entry.get("kind") or ""),
            )
            labels[unit_id] = label
            if record_cache:
                _append_record(
                    target,
                    {
                        "v": PAGE_RECOMPILE_LEDGER_VERSION,
                        "kind": LABEL_KIND,
                        "ts": now_iso(),
                        "label": label.to_dict(),
                    },
                )
    return labels


# ---------------------------------------------------------------------------
# AC2/AC4: the plan pass.
# ---------------------------------------------------------------------------


def _render_labels_for_prompt(labels: Mapping[str, UnitLabel]) -> str:
    lines = [
        f"[{label.unit_id}] topic={label.topic!r} belongs={label.belongs!r} "
        f"about={label.about!r} kind={label.kind!r}"
        for label in sorted(labels.values(), key=lambda label: label.unit_id)
    ]
    return fence_untrusted(
        "\n".join(lines), tag=_LABELS_TAG, max_chars=_SUMMARY_FENCE_MAX_CHARS
    )


def _render_shortlist_for_prompt(shortlist: Sequence[PageCandidate]) -> str:
    lines = [f"{candidate.uid}\t{candidate.name}" for candidate in shortlist]
    return fence_untrusted(
        "\n".join(lines), tag=_SHORTLIST_TAG, max_chars=_SUMMARY_FENCE_MAX_CHARS
    )


def build_plan_user_message(
    segment: SegmentResult,
    labels: Mapping[str, UnitLabel],
    shortlist: Sequence[PageCandidate],
    *,
    page_name: str,
    page_type: str,
    body: str,
) -> str:
    """The plan call's user message. Every untrusted part goes through
    :func:`fence_untrusted` (AC3) -- nothing is interpolated raw."""
    definitions = parse_footnote_definitions(body)
    definition_lines = "\n".join(
        f"[^{label}]: {definitions[label]}" for label in sorted(definitions)
    )
    return "\n\n".join(
        [
            fence_untrusted(
                f"name: {page_name}\ntype: {page_type}",
                tag=_META_TAG,
                max_chars=_SUMMARY_FENCE_MAX_CHARS,
            ),
            _render_units_for_prompt(segment.units),
            _render_labels_for_prompt(labels),
            _render_shortlist_for_prompt(shortlist),
            fence_untrusted(
                definition_lines, tag=_DEFINITIONS_TAG, max_chars=_SUMMARY_FENCE_MAX_CHARS
            ),
        ]
    )


def propose_plan(
    segment: SegmentResult,
    labels: Mapping[str, UnitLabel],
    shortlist: Sequence[PageCandidate],
    client: Any,
    *,
    page_name: str,
    page_type: str,
    body: str,
    config: dict[str, Any] | None = None,
    usage: TokenUsage | None = None,
) -> dict[str, Any] | None:
    """Ask the ``page_recompile`` knob for groups, routing and the summary (AC2)."""
    if client is None:
        return None
    return _call_model(
        client,
        model=_get_page_recompile_model(config),
        system=PAGE_RECOMPILE_PLAN_SYSTEM,
        user_message=build_plan_user_message(
            segment, labels, shortlist, page_name=page_name, page_type=page_type, body=body
        ),
        max_tokens=_PLAN_MAX_TOKENS,
        max_tokens_env="ATHENAEUM_PAGE_RECOMPILE_MAX_TOKENS",
        thinking_env="ATHENAEUM_PAGE_RECOMPILE_THINKING",
        budget_knob=PLAN_KNOB,
        usage_knob=PLAN_KNOB,
        config=config,
        usage=usage,
        description="page_recompile_plan",
    )


# ---------------------------------------------------------------------------
# AC9: the verify pass.
# ---------------------------------------------------------------------------


def build_verify_user_message(plan: RecompilePlan, segment: SegmentResult) -> str:
    """The verify call's user message: the numbered sentences and, for each,
    the units that DEFINE the labels it cites. Both fenced (AC3)."""
    sentences = split_summary_sentences(plan.summary)
    numbered = "\n".join(f"{index}. {sentence}" for index, sentence in enumerate(sentences))
    wanted = {label for sentence in sentences for label in cited_labels(sentence)}
    relevant = [
        unit
        for unit in segment.units
        if wanted & set(unit.footnote_labels)
    ]
    return "\n\n".join(
        [
            fence_untrusted(numbered, tag=_SUMMARY_TAG, max_chars=_SUMMARY_FENCE_MAX_CHARS),
            _render_units_for_prompt(relevant or list(segment.units)),
        ]
    )


def _unusable_verify(plan: RecompilePlan, detail: str) -> RecompilePlan:
    """A verify that was MADE and produced nothing usable (AC9).

    Marks the attempt, leaves ``verified`` ``None`` (no verdict was
    obtained -- the field says what the model concluded, and it concluded
    nothing), and records the reason as a :data:`VERIFY_UNUSABLE_CODE`
    issue, which is what makes :attr:`RecompilePlan.valid` fail closed.
    """
    log.warning("page_recompile_plan: verify unusable (%s)", detail)
    return replace(
        plan,
        issues=plan.issues + (PlanIssue(VERIFY_UNUSABLE_CODE, detail),),
        verified=None,
        verify_attempted=True,
    )


def verify_summary(
    plan: RecompilePlan,
    segment: SegmentResult,
    client: Any,
    *,
    config: dict[str, Any] | None = None,
    usage: TokenUsage | None = None,
) -> RecompilePlan:
    """Check each summary sentence on the ``verify`` knob (AC9).

    Three outcomes, kept deliberately distinct:

    * **Not requested** -- *client* is ``None``. The plan is returned
      untouched: ``verified`` ``None``, ``verify_attempted`` ``False``, no
      issue. A caller that did not ask for a check does not get a failure
      for not asking.
    * **Attempted and answered** -- ``verified`` becomes ``True``/``False``.
      A sentence the model calls ``unsupported``, or one it answers nothing
      about, adds an issue and marks the plan invalid.
    * **Attempted and unusable** -- the call errored, or came back with no
      ``sentences`` array, or with an empty one. ``verify_attempted`` is
      ``True``, ``verified`` stays ``None``, and a
      :data:`VERIFY_UNUSABLE_CODE` issue is added so the plan is INVALID.
      This path used to be indistinguishable from "not requested", which
      left ``valid`` ``True`` for a summary nothing had confirmed -- a trap
      for the applier slice, and inconsistent with the per-sentence rule a
      few lines below, which already treats silence as failure.

    This never rewrites the summary.
    """
    if client is None:
        return plan
    payload = _call_model(
        client,
        model=_get_verify_model(config),
        system=PAGE_RECOMPILE_VERIFY_SYSTEM,
        user_message=build_verify_user_message(plan, segment),
        max_tokens=_VERIFY_MAX_TOKENS,
        max_tokens_env="ATHENAEUM_PAGE_RECOMPILE_VERIFY_MAX_TOKENS",
        thinking_env="ATHENAEUM_PAGE_RECOMPILE_VERIFY_THINKING",
        budget_knob="page_recompile_verify",
        usage_knob=VERIFY_KNOB,
        config=config,
        usage=usage,
        description="page_recompile_verify",
    )
    if payload is None:
        return _unusable_verify(plan, "the verify call returned no usable response")
    raw = payload.get("sentences")
    if not isinstance(raw, list):
        return _unusable_verify(plan, "the verify response carried no sentences array")
    if not raw:
        return _unusable_verify(
            plan, "the verify response carried an empty sentences array"
        )

    sentences = split_summary_sentences(plan.summary)
    unsupported: list[PlanIssue] = []
    seen_indexes: set[int] = set()
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        try:
            index = int(entry.get("index"))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        if not 0 <= index < len(sentences):
            continue
        seen_indexes.add(index)
        if str(entry.get("verdict") or "").strip() != "supported":
            unsupported.append(PlanIssue("unsupported-sentence", sentences[index]))

    missing = [i for i in range(len(sentences)) if i not in seen_indexes]
    if missing:
        # A verdict-less sentence is not a passing one: the check exists to
        # confirm EVERY sentence, so silence reads as a failure, not a pass.
        unsupported.extend(PlanIssue("unverified-sentence", sentences[i]) for i in missing)

    return replace(
        plan,
        issues=plan.issues + tuple(unsupported),
        verified=not unsupported,
        verify_attempted=True,
    )


# ---------------------------------------------------------------------------
# AC11: the orchestrator the CLI calls.
# ---------------------------------------------------------------------------


def page_content_hash(body: str) -> str:
    """The content hash a plan is pinned to (AC11): sha256 over the page BODY.

    The body, not the whole file: frontmatter carries an ``updated:`` stamp
    and other bookkeeping that moves without the content moving, and a plan
    is about the content.
    """
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


@dataclass
class PlanRunResult:
    """What :func:`plan_page` produced, for the CLI to report."""

    plan: RecompilePlan | None
    plan_id: str = ""
    labels: dict[str, UnitLabel] = field(default_factory=dict)
    shortlist: tuple[PageCandidate, ...] = ()
    usage: TokenUsage = field(default_factory=TokenUsage)
    error: str = ""


def plan_page(
    *,
    wiki_root: Path,
    uid: str,
    client: Any,
    config: dict[str, Any] | None = None,
    index: EntityIndex | None = None,
    ledger_path: Path | None = None,
    record: bool = True,
    verify: bool = True,
    usage: TokenUsage | None = None,
) -> PlanRunResult:
    """Plan one page end to end and append the shadow record (AC11).

    *usage* (issue athenaeum#2547): the caller's own accumulator, so a
    caller that makes several plan calls in one run -- the nightly phase --
    gets ONE total with every call still tagged by the knob that made it
    (:meth:`athenaeum.models.TokenUsage.add`'s ``knob=``), and the run-level
    ``max_api_calls`` ceiling and the spend ledger both see this function's
    calls. Omitted, a fresh :class:`~athenaeum.models.TokenUsage` is used
    and returned on :attr:`PlanRunResult.usage`, which is what every
    pre-athenaeum#2547 caller already does.

    Reads the page, decides whether this pass may act on it at all
    (:func:`athenaeum.page_recompile.classify_page_text`), segments it with
    :func:`athenaeum.page_recompile.segment_page` (never re-implemented
    here), labels, proposes, validates, verifies, and appends. **Writes no
    page** -- the only file this function can write is the ledger.

    **Why the eligibility check reuses S1's own predicates.** Issue
    athenaeum#2536 extracted ``find_recompile_candidates``'s loop body into
    :func:`~athenaeum.page_recompile.classify_page_text` precisely so a
    caller already holding a page's text need not grow a second copy of
    them. This function is such a caller: it reaches its page by uid
    through :class:`~athenaeum.models.EntityIndex`, which indexes
    tombstoned, retired and superseded pages like any other -- so without
    that call it would happily plan the recompilation of a page the S1 scan
    exists to exclude (see that module's docstring on phantom candidates).
    A ``BlockedPage`` outcome is refused by its own reason.

    An eligible-but-under-threshold page is deliberately still planned: a
    ``None`` outcome means EITHER inactive OR simply not large enough, and
    only the first is a refusal -- an operator naming a uid explicitly has
    asked about that page, and "not currently over the threshold" is not a
    reason to decline. The two are told apart with
    :func:`~athenaeum.page_relations.is_inactive_relation_target`, the same
    single predicate ``classify_page_text`` itself uses, not a second
    tombstone check.
    """
    from athenaeum.models import parse_frontmatter
    from athenaeum.page_recompile import BlockedPage, classify_page_text, segment_page
    from athenaeum.page_relations import is_inactive_relation_target

    usage = usage if usage is not None else TokenUsage()
    page_index = index if index is not None else EntityIndex(wiki_root)
    path = page_index.get_by_uid(uid)
    if path is None:
        return PlanRunResult(plan=None, usage=usage, error=f"no page with uid {uid}")
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return PlanRunResult(plan=None, usage=usage, error=f"unreadable page: {exc}")

    meta, body = parse_frontmatter(text)
    meta_dict = meta if isinstance(meta, dict) else {}

    outcome = classify_page_text(
        path,
        text,
        threshold=resolve_page_size_threshold_chars(config),
        ceiling=resolve_oversize_hard_ceiling_chars(config),
    )
    if isinstance(outcome, BlockedPage):
        return PlanRunResult(
            plan=None, usage=usage, error=f"page is blocked ({outcome.reason})"
        )
    if outcome is None and is_inactive_relation_target(meta_dict):
        return PlanRunResult(
            plan=None,
            usage=usage,
            error="page is inactive (tombstoned, retired or superseded)",
        )

    # AC6 of issue athenaeum#2541: an undone apply is not re-drafted while
    # the page still holds the restored pre-image. Checked BEFORE the
    # labelling pass, so an undone page costs no model call at all.
    undone = undone_page_skip_reason(wiki_root, uid=uid, body=body, ledger_path=ledger_path)
    if undone:
        return PlanRunResult(plan=None, usage=usage, error=undone)

    segment = segment_page(body)
    if not segment.units:
        return PlanRunResult(plan=None, usage=usage, error="page has no units to plan")

    labels = label_units(
        segment.units,
        client,
        wiki_root=wiki_root,
        config=config,
        usage=usage,
        ledger_path=ledger_path,
        record_cache=record,
    )
    shortlist = build_page_shortlist(labels.values(), page_index, self_uid=uid)
    payload = propose_plan(
        segment,
        labels,
        shortlist,
        client,
        page_name=str(meta_dict.get("name") or path.stem),
        page_type=str(meta_dict.get("type") or ""),
        body=body,
        config=config,
        usage=usage,
    )
    if payload is None:
        return PlanRunResult(
            plan=None, labels=labels, shortlist=shortlist, usage=usage, error="no plan returned"
        )

    plan = validate_plan(
        payload,
        segment,
        body,
        uid=uid,
        content_hash=page_content_hash(body),
        known_uids=[candidate.uid for candidate in shortlist],
    )
    # AC5 of issue athenaeum#2548: a ``standalone`` group whose proposed
    # name already names an existing page (by name OR by alias) is a MOVE,
    # not a new page. Resolved here, before the record is appended, so the
    # plan an operator answers states the action that will be taken -- see
    # :func:`resolve_standalone_groups` for why this is the planner's job
    # and not the applier's.
    plan = resolve_standalone_groups(plan, page_index)
    if verify and plan.summary:
        plan = verify_summary(plan, segment, client, config=config, usage=usage)

    plan_id = ""
    if record:
        plan_id = append_plan_record(plan, wiki_root, ledger_path=ledger_path)
    else:
        plan_id = plan.plan_id()
    return PlanRunResult(
        plan=plan, plan_id=plan_id, labels=labels, shortlist=shortlist, usage=usage
    )


__all__ = [
    "ALIAS_MOVE_VERB",
    "ARG_DISPOSITIONS",
    "BARE_DISPOSITIONS",
    "LABEL_CHUNK_UNITS",
    "LABEL_KIND",
    "LABEL_KNOB",
    "PAGE_ACTIONS",
    "PAGE_RECOMPILE_LABEL_SYSTEM",
    "PAGE_RECOMPILE_LEDGER_FILENAME",
    "PAGE_RECOMPILE_LEDGER_VERSION",
    "PAGE_RECOMPILE_PLAN_SYSTEM",
    "PAGE_RECOMPILE_VERIFY_SYSTEM",
    "PLAN_KIND",
    "PLAN_KNOB",
    "SCOPE_CROSS_PAGE",
    "SCOPE_STRUCTURE_ONLY",
    "STANDALONE_NAME_INACTIVE_CODE",
    "STANDALONE_NAME_IS_SOURCE_CODE",
    "STANDALONE_NAME_NO_UID_CODE",
    "STRUCTURE_ONLY_VERBS",
    "UNDO_KIND",
    "VERIFY_KNOB",
    "VERIFY_UNUSABLE_CODE",
    "MergedSection",
    "PageCandidate",
    "PlanGroup",
    "PlanIssue",
    "PlanRunResult",
    "RecompilePlan",
    "UnitLabel",
    "append_plan_record",
    "build_page_shortlist",
    "build_plan_user_message",
    "build_verify_user_message",
    "check_summary_citations",
    "cited_labels",
    "classify_plan_scope",
    "default_page_recompile_ledger_path",
    "find_plan_record",
    "label_units",
    "normalise_unit_text",
    "page_content_hash",
    "parse_alias_move",
    "plan_page",
    "propose_plan",
    "read_label_cache",
    "read_page_recompile_ledger",
    "resolve_standalone_groups",
    "split_disposition",
    "split_summary_sentences",
    "summary_definition_labels",
    "undone_page_skip_reason",
    "unit_hash",
    "validate_plan",
    "verify_summary",
]
