# SPDX-License-Identifier: Apache-2.0
"""Underdetermined-source (Tier-3 CREATE) live-API eval (issue athenaeum#1518).

Runs :func:`athenaeum.tiers.tier3_create` against every case in
``tests/evals/data/underdetermined/cases.yaml`` using a real Anthropic call
(the same WRITE model tier3_create uses in production, ``DEFAULT_WRITE_MODEL``).

Every existing golden set that drives tier3_create/tier3_merge is
conflict-shaped: ``merge/cases.yaml``'s four cases are all two-or-more
claims competing, and ``tests/evals/tier_compare.py`` scores each case for
an embedded probe rather than dedicating a case to underdetermination. None
of them asks what happens when a source simply says NOTHING about a field
the entity template invites — the gap this layer fills. ``tests/test_tiers.py``
proves the PARSER handles canned responses of each shape; it cannot show
whether the live model, given a source that underdetermines a field, still
tends to (a) leave that field absent rather than infer it from context such
as the authoring document's own provenance, (b) avoid folding an adjacent
first-person-author fact into a different named person's attributed role,
(c) raise a materially-gapped field via the existing `## Open Questions`
mechanism instead of guessing OR staying silent, and (d) still populate a
field the source DOES state plainly — the negative control proving the fix
is not blanket suppression. That's exactly the "output shape / a scoring
judgment a unit test can't pin" gap this eval fills.

Scoring is over RESULT SHAPE (substring presence/absence, same-sentence
co-occurrence as a proxy for "is this fact attributed to this person") —
never exact prose.

Per-case outcomes are appended to the session accumulator; the aggregate
pass floor is asserted in :func:`test_underdetermined_aggregate_floor` — a
single miss does NOT flake main, but a systemic degradation does.

Marker: ``pytest.mark.eval`` — deselected by default (see pyproject).
"""

from __future__ import annotations

import re
from typing import Any

import pytest
import yaml

from athenaeum.models import EntityAction, TokenUsage
from athenaeum.tiers import DEFAULT_WRITE_MODEL, tier3_create
from tests.evals.harness import (
    EVAL_DATA_ROOT,
    LAYER_UNDERDETERMINED,
    RecordingClient,
    build_live_client,
    live_ready,
)

pytestmark = pytest.mark.eval


# Floor derivation: N=4 cases, one per outcome class (silent_field_absent,
# adjacent_fact_not_folded, material_gap_escalates, stated_field_populates).
# A floor of 3 leaves 1-case slack over an all-pass expectation, matching
# MERGE_FLOOR's reasoning exactly (tests/evals/test_merge_eval.py) — the
# smallest golden set in the suite, so a single hard case does not sink the
# whole layer, but a systemic miss (e.g. the negative control D starts
# failing, meaning the fix regressed to blanket suppression) fails loudly.
UNDERDETERMINED_FLOOR = 3  # >= 3/4


def _load_cases() -> list[dict[str, Any]]:
    cases_path = EVAL_DATA_ROOT / "underdetermined" / "cases.yaml"
    return list(yaml.safe_load(cases_path.read_text(encoding="utf-8")))


def _make_action(case: dict[str, Any]) -> EntityAction:
    entity = case["entity"]
    return EntityAction(
        kind="create",
        name=str(entity["name"]),
        entity_type=str(entity["entity_type"]),
        tags=list(entity.get("tags") or []),
        access=str(entity.get("access", "internal")),
        existing_uid=None,
        observations=str(case["observation"]),
    )


_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")

# Inline footnote reference markers (``[^1]``, and consecutive ones like
# ``[^1][^2]``) that the librarian writes immediately after the sentence
# terminator (athenaeum#1730: ``...approved all three layouts.[^1] Separately,
# ...``). They carry no attribution meaning of their own, but sitting between
# the ``.`` and the following whitespace defeats ``_SENTENCE_SPLIT``'s
# lookbehind, which requires whitespace immediately after ``[.!?]`` — so two
# sentences silently score as one and same-sentence co-occurrence checks fire
# on facts that were never in the same sentence. Stripping the markers before
# splitting fixes this without touching footnote DEFINITION lines
# (``[^1]: sessions/...``): those are already isolated onto their own
# sentence by the surrounding blank line (``\n+``), independent of whether
# the leading marker itself is stripped.
_FOOTNOTE_MARKER = re.compile(r"\[\^[^\]\s]+\]")


def _sentences(text: str) -> list[str]:
    stripped = _FOOTNOTE_MARKER.sub("", text)
    return [s for s in _SENTENCE_SPLIT.split(stripped) if s.strip()]


def _co_occurs_same_sentence(body: str, a: str, b: str) -> bool:
    """Whether *a* and *b* both appear (case-insensitively) in one sentence.

    A proxy for "is fact b attributed to a" that scores shape, not exact
    prose: it does not care HOW the sentence says it, only whether the two
    substrings ever land in the same sentence.
    """
    a_l, b_l = a.lower(), b.lower()
    return any(a_l in s.lower() and b_l in s.lower() for s in _sentences(body))


# Affiliation-assertion proxy (athenaeum#1668).
#
# ``silent_affiliation_not_inferred`` originally scored "no affiliation
# invented" as a blanket ban on the authoring org's name appearing anywhere
# in the body (``must_not_include_substrings: ["Larkspur"]``), on the stated
# premise that "the authoring org's name has no reason to appear in the
# composed body at all". That premise is false: the observation's own first
# line IS the source document's header ("Larkspur Site Audits — Walkthrough
# Log, 2026-04-02"), so restating the document's provenance is a legitimate,
# non-inferential use of the name. The ban could not tell the wrong
# inference ("Devon Achebe (Larkspur Site Audits) flagged ...") apart from
# correct provenance ("... as part of a Larkspur Site Audits log, Devon
# Achebe flagged ..."), and the case consequently failed every run from the
# day it landed. See tests/test_underdetermined_scoring.py for the
# discriminator's own positive/negative table, which pins a recorded body
# that exhibits the target behaviour and used to score as a miss.
#
# This predicate instead looks for the SYNTAX of an affiliation claim: the
# org in an appositive, prepositional or possessive construction bound to
# the person, within one sentence.

# Closed list of role nouns that, placed between an org and a person, assert
# affiliation ("Larkspur Site Audits consultant Devon Achebe"). Deliberately
# closed: an open "any word" rule matches provenance phrasing such as
# "a Larkspur Site Audits log, Devon Achebe", which is not a claim about the
# person at all.
_ROLE_NOUNS = (
    "consultant|auditor|engineer|analyst|manager|director|lead|partner|"
    "associate|employee|staffer|representative|rep|contractor|principal|"
    "specialist|technician|inspector|surveyor|architect|coordinator|"
    "officer|owner|founder|advisor|adviser"
)

# Optional article/determiner plus the org name's own trailing words and up
# to a couple of modifiers ("Larkspur[ Site Audits senior] consultant"). The
# closed role-noun list, not this word budget, is what keeps provenance
# phrasing out: "a Larkspur Site Audits log, Devon Achebe" ends in "log",
# which is not a role noun, so it never matches however wide the gap is.
#
# Horizontal whitespace only (``[^\S\n]``, not ``\s``): a sentence-ending
# ``.!?`` already cannot appear inside ``\w+,?``, but a bare line break can,
# and "... filed by Larkspur Site Audits\nLead Devon Achebe" is two separate
# statements, not an affiliation claim. Every other pattern below is bounded
# by _NO_STOP, which excludes newlines for the same reason.
_HSPACE = r"[^\S\n]+"
_MODS = rf"(?:(?:a|an|the|our|their|its|his|her){_HSPACE})?(?:\w+,?{_HSPACE}){{0,4}}"

# No sentence terminator may fall inside a gap — the claim has to be made in
# one sentence, matching _co_occurs_same_sentence's proxy for attribution.
_NO_STOP = r"[^.!?\n]"


def _affiliation_patterns(person: str, org: str) -> list[str]:
    p, o = re.escape(person), re.escape(org)
    return [
        # "Devon Achebe (Larkspur ...)" — parenthetical affiliation
        rf"{p}\s*,?\s*\({_NO_STOP}{{0,30}}?{o}",
        # "Devon Achebe, a senior consultant at Larkspur ..." /
        # "Devon Achebe of Larkspur ..." — prepositional affiliation
        rf"{p}\s*(?:,\s*)?{_NO_STOP}{{0,40}}?\b(?:of|at|with|from)\s+"
        rf"(?:(?:a|an|the)\s+)?{o}",
        # "Devon Achebe, Larkspur Site Audits" — bare appositive
        rf"{p}\s*,\s*(?:(?:a|an|the)\s+)?{o}",
        # "Devon Achebe's Larkspur ..." — possessive on the person
        rf"{p}['’]s\s+{_NO_STOP}{{0,20}}?{o}",
        # "Larkspur's Devon Achebe" — possessive on the org
        rf"{o}{_NO_STOP}{{0,20}}?['’]s\s+{_NO_STOP}{{0,20}}?{p}",
        # "Larkspur Site Audits consultant Devon Achebe" — role-noun apposition
        rf"{o}{_HSPACE}{_MODS}(?:{_ROLE_NOUNS})s?{_HSPACE}{p}",
    ]


def _asserts_affiliation(body: str, person: str, org: str) -> bool:
    """Whether *body* claims *person* is affiliated with *org*.

    True only for an affiliation CONSTRUCTION (appositive, prepositional or
    possessive) inside a single sentence — not for a mere co-mention, and
    not for provenance ("... as part of a Larkspur Site Audits log, Devon
    Achebe flagged ..."), which names the source document rather than the
    person's employer.
    """
    return any(
        re.search(pattern, body or "", re.IGNORECASE)
        for pattern in _affiliation_patterns(person, org)
    )


def _score_case(case: dict[str, Any], body: str | None) -> tuple[bool, str]:
    expected = case["expected"]
    reasons: list[str] = []
    haystack = (body or "").lower()

    for substr in expected.get("must_include_substrings", []):
        if substr.lower() not in haystack:
            reasons.append(f"missing expected substring {substr!r}")

    for substr in expected.get("must_not_include_substrings", []):
        if substr.lower() in haystack:
            reasons.append(f"unexpected substring {substr!r} present (invented fact?)")

    for pair in expected.get("forbidden_affiliation", []):
        person, org = pair[0], pair[1]
        if _asserts_affiliation(body or "", person, org):
            reasons.append(
                f"body asserts {person!r} is affiliated with {org!r} — an "
                "affiliation the source never states (invented fact?)"
            )

    for pair in expected.get("forbidden_co_occurrence", []):
        a, b = pair[0], pair[1]
        if _co_occurs_same_sentence(body or "", a, b):
            reasons.append(
                f"{a!r} and {b!r} co-occur in one sentence — looks like a "
                "fact folded into the wrong person's role"
            )

    passed = not reasons
    detail = "; ".join(reasons) if reasons else "ok"
    return passed, detail


@pytest.fixture(scope="module")
def _live_ready() -> None:
    ok, reason = live_ready()
    if not ok:
        pytest.skip(reason)


@pytest.mark.parametrize("case", _load_cases(), ids=lambda c: c["id"])
def test_underdetermined_case(
    case: dict[str, Any],
    eval_record: bool,
    eval_session: Any,
    _live_ready: None,
) -> None:
    """Run one underdetermined-source case; record its outcome for the
    aggregate score.

    Individual case failure does NOT fail the test — the aggregate floor
    (see :func:`test_underdetermined_aggregate_floor`) does.
    """
    action = _make_action(case)

    inner = build_live_client()
    client = RecordingClient(inner, record=eval_record, layer=LAYER_UNDERDETERMINED)
    client.start_case(case["id"])

    original_create = client.messages.create

    def _create(**params: Any) -> Any:
        response = original_create(**params)
        eval_session.observe_response(str(params.get("model", "")), response)
        return response

    client.messages.create = _create  # type: ignore[method-assign]

    usage = TokenUsage()
    entity = tier3_create(
        action,
        str(case["source_ref"]),
        client,
        usage=usage,
    )
    client.end_case()

    passed, detail = _score_case(case, entity.body)

    eval_session.record_case(
        LAYER_UNDERDETERMINED,
        case["id"],
        expected=str(case["expected"]),
        observed=f"body_len={len(entity.body or '')}",
        passed=passed,
        detail=f"outcome_class={case.get('outcome_class', '')} {detail}",
    )


def test_underdetermined_aggregate_floor(eval_session: Any, _live_ready: None) -> None:
    """Assert the underdetermined-source layer meets the aggregate floor."""
    passed, total = eval_session.layer_score(LAYER_UNDERDETERMINED)
    assert total > 0, "underdetermined eval collected no cases"
    assert passed >= UNDERDETERMINED_FLOOR, (
        f"underdetermined below aggregate floor: {passed}/{total} "
        f"(need >= {UNDERDETERMINED_FLOOR}). Model: {DEFAULT_WRITE_MODEL}. "
        "Check eval-summary.json for per-case failures."
    )
