# SPDX-License-Identifier: Apache-2.0
"""Structural grading for the person-hint eval (issue athenaeum#1867).

The question this layer exists to answer, which no other layer asks: given a
wiki that already has a page for a known PERSON, and a new raw file that
NAMES them, does that person's page gain a claim only when the file actually
ASSERTS something about them -- and is the rest of the file still compiled?

``tests/evals/attachment.py`` (issue athenaeum#1580) grades the same entry
point, ``librarian.process_one``, but passes no ``person_registry=``. Tier 0's
person-registry consult therefore never engages on that layer, and the
decision this module grades is invisible to it. Everything here is offline and
deterministic; the metered half lives in
``tests/evals/test_person_hint_eval.py``. That split is what lets the grader
be unit-tested in ordinary CI (``tests/evals/test_person_hint_grading.py``)
rather than only exercised on a live run -- the same division
``attachment.py`` already draws, and :func:`snapshot_wiki`, :func:`diff_wiki`
and :func:`attribute_tier` are IMPORTED from there rather than reimplemented.

Three things in here are load-bearing.

**1. The anti-vacuity trap: on the shipped path the person page DOES change.**
Tier 0 (``librarian.py``'s person-registry consult) claims any raw file that
names a known person and prepends a dated Notes bullet to that person's page
(``intake.attribute_person_observation``). A grader that asked "did the person
page change" would score the shipped path as a PASS on every case in this
layer. So :func:`classify_person_outcome` does not ask whether the page
changed -- it asks HOW, and tells four documented shapes apart:

``unchanged``
    The body is byte-identical. The right answer for a passing mention.
``notes_bullet``
    A dated ``- YYYY-MM-DD: <excerpt> (source: <ref>)`` bullet appeared --
    the shipped tier-0 shape. **Always a failure**, in every case, expected
    or not: it is an excerpt of a raw file pasted onto a person page, not a
    claim the page now makes.
``citation_only``
    Exactly one footnote DEFINITION line was appended and nothing else
    moved -- the shape ``tiers._append_source_citation`` produces when the
    write tier reports ``adds_new_claim: false``.
``footnoted_claim``
    A new footnote definition citing the raw ref, AND at least one new
    inline ``[^N]`` marker in the body: the page now makes a claim and says
    where it came from.

``decided_by`` reports ``write_merge`` for BOTH of the last two, so the tier
label alone cannot separate "cited the source" from "made a claim". The page
delta is the second, required observable, and that is why this module exists
alongside :func:`attribute_tier` rather than being folded into it.

**2. The four shapes are not exhaustive over arbitrary body edits, and the
residual must not be laundered into one of them.** A write tier can rewrite a
body with a new sentence and no footnote at all. That is neither
``citation_only`` (more than one appended definition line moved) nor
``footnoted_claim`` (nothing cites the ref), and calling it either would score
an UNCITED claim as a correctly-sourced one -- the same vacuity the four
shapes exist to prevent, reintroduced at the bottom of the classifier. It is
reported as :data:`OUTCOME_UNCITED_CHANGE` and scored as a failure wherever it
appears. The classifier stays single-valued: every delta gets exactly one
label.

**3. A person page is not the only thing a raw file should produce.** The
shipped tier-0 path early-returns the moment it attributes an observation, so
the file is claimed WHOLE and tiers 1-3 never see it: nothing else in the file
is compiled. A layer that graded only person pages would be blind to that,
which is the failure it most needs to catch. :func:`score_case`'s
``require_non_person_pointer`` is the compile requirement -- at least one
page whose ``type`` is not ``person`` must have been created or changed
carrying a pointer back to the raw ref. A run whose only change is on person
pages fails.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from athenaeum.models import parse_frontmatter

# Re-exported so this layer's call sites import ONE grader module. These are
# attachment.py's, deliberately not copies -- see the module docstring.
from tests.evals.attachment import (  # noqa: F401  (re-export)
    PageState,
    TierAttribution,
    WikiDelta,
    WikiSnapshot,
    attribute_tier,
    diff_wiki,
    snapshot_wiki,
)

#: The page type the person registry consults, and the type this layer grades
#: person-page outcomes for. Mirrors
#: :data:`athenaeum.person_registry.PERSON_TYPE`; kept as a local literal so
#: the grader's notion of "non-person page" does not silently follow a change
#: to the registry's scope without a test noticing.
PERSON_TYPE = "person"

# ---------------------------------------------------------------------------
# The four graded outcomes, plus the residual
# ---------------------------------------------------------------------------

OUTCOME_UNCHANGED = "unchanged"
OUTCOME_NOTES_BULLET = "notes_bullet"
OUTCOME_CITATION_ONLY = "citation_only"
OUTCOME_FOOTNOTED_CLAIM = "footnoted_claim"
#: The residual (see the module docstring, point 2). NOT one of the four
#: graded shapes and never an acceptable expectation -- a case that expects it
#: is rejected by :func:`validate_case_spec`.
OUTCOME_UNCITED_CHANGE = "uncited_change"

#: The four shapes a case may legitimately expect.
GRADED_OUTCOMES = (
    OUTCOME_UNCHANGED,
    OUTCOME_NOTES_BULLET,
    OUTCOME_CITATION_ONLY,
    OUTCOME_FOOTNOTED_CLAIM,
)

#: Every label :func:`classify_person_outcome` can return.
ALL_OUTCOMES = GRADED_OUTCOMES + (OUTCOME_UNCITED_CHANGE,)

#: The shipped tier-0 bullet, as ``intake.attribute_person_observation``
#: writes it: ``- YYYY-MM-DD: <excerpt> (source: <ref>)`` prepended under
#: ``## Notes``. ``DOTALL`` is load-bearing and was measured, not assumed --
#: the excerpt is a bounded slice of the raw body and routinely spans several
#: lines, so the closing ``(source: ...)`` is usually NOT on the same line as
#: the date. A single-line pattern would miss the shipped shape entirely and
#: hand this layer a silent pass on exactly the outcome it exists to fail.
_NOTES_BULLET_RE = re.compile(
    r"^-[ \t]*(\d{4}-\d{2}-\d{2}):[ \t].*?\(source:[^)]*\)",
    re.MULTILINE | re.DOTALL,
)

#: A footnote DEFINITION line (``[^1]: ...``) -- the same shape
#: ``tiers._FOOTNOTE_DEF_RE`` picks the next citation number from.
_FOOTNOTE_DEF_RE = re.compile(r"^\[\^([^\]]+)\]:[ \t]*(.*)$", re.MULTILINE)

#: An INLINE footnote marker (``[^1]`` in prose), i.e. one NOT followed by a
#: colon. The negative lookahead is what separates a claim's marker from the
#: definition that resolves it.
_FOOTNOTE_MARKER_RE = re.compile(r"\[\^([^\]]+)\](?!:)")


def _norm(body: str) -> str:
    """Trailing-whitespace-insensitive body text.

    A write path that re-renders a page can add or drop a trailing newline
    without changing a single claim. Grading that as a change would report
    ``uncited_change`` for a no-op.
    """
    return body.replace("\r\n", "\n").rstrip("\n")


def _footnote_defs(body: str) -> list[tuple[str, str]]:
    return [(m.group(1), m.group(2).strip()) for m in _FOOTNOTE_DEF_RE.finditer(body)]


def _inline_markers(body: str) -> list[str]:
    return [m.group(1) for m in _FOOTNOTE_MARKER_RE.finditer(body)]


def classify_person_outcome(before_body: str, after_body: str, raw_ref: str) -> str:
    """Classify one person page's before/after body delta (AC1).

    Returns exactly one of :data:`ALL_OUTCOMES`. Precedence is
    most-damning-first, mirroring :class:`~tests.evals.attachment.
    TierAttribution`'s most-expensive-wins rule and for the same reason: a run
    that pasted a raw excerpt onto a person page AND cited it has still pasted
    a raw excerpt onto a person page, and the metric must show that rather
    than let the tidier half of the edit absorb it.

    *raw_ref* is the intake file's :attr:`athenaeum.models.RawFile.ref`.
    ``footnoted_claim`` requires a citation naming it specifically: a page
    that grew a footnote to some OTHER source did not record where THIS
    file's claim came from.
    """
    before = _norm(before_body)
    after = _norm(after_body)

    if before == after:
        return OUTCOME_UNCHANGED

    # The added text, for the bullet check. A prepend under ``## Notes`` is
    # not a suffix of the page, so this cannot be a plain suffix diff.
    before_lines = before.split("\n")
    after_lines = after.split("\n")
    before_counts: dict[str, int] = {}
    for line in before_lines:
        before_counts[line] = before_counts.get(line, 0) + 1
    added_lines: list[str] = []
    for line in after_lines:
        if before_counts.get(line, 0) > 0:
            before_counts[line] -= 1
        else:
            added_lines.append(line)
    added = "\n".join(added_lines)

    if _NOTES_BULLET_RE.search(added):
        return OUTCOME_NOTES_BULLET

    # Citation-only, as ``tiers._append_source_citation`` builds it: the body
    # is byte-identical apart from ONE appended footnote definition. Tested as
    # a strict prefix + single-line remainder rather than by counting
    # definitions, because "gained one definition" is also true of a rewrite
    # that happened to net one.
    if after.startswith(before):
        remainder = after[len(before) :].strip()
        if remainder and len(_footnote_defs(remainder)) == 1:
            if remainder == _FOOTNOTE_DEF_RE.search(remainder).group(0).strip():  # type: ignore[union-attr]
                return OUTCOME_CITATION_ONLY

    new_defs = [d for d in _footnote_defs(after) if d not in _footnote_defs(before)]
    cites_raw = any(raw_ref and raw_ref in text for _, text in new_defs)
    gained_marker = len(_inline_markers(after)) > len(_inline_markers(before))
    if cites_raw and gained_marker:
        return OUTCOME_FOOTNOTED_CLAIM

    return OUTCOME_UNCITED_CHANGE


# ---------------------------------------------------------------------------
# Body capture
# ---------------------------------------------------------------------------


def read_page_bodies(wiki_root: Path) -> dict[str, str]:
    """Read *wiki_root* into ``{uid: body}``.

    :func:`~tests.evals.attachment.snapshot_wiki` deliberately keeps only a
    ``body_digest`` -- "did this page change", never what it says -- because
    an attachment result must not be hostage to model wording. This layer
    needs the opposite: telling a dated Notes bullet from a footnoted claim
    is a question about the SHAPE of the text, so the text has to survive.
    The two are taken side by side; neither replaces the other.
    """
    bodies: dict[str, str] = {}
    if not wiki_root.is_dir():
        return bodies
    for path in sorted(wiki_root.glob("*.md")):
        if path.name.startswith("_"):
            continue
        meta, body = parse_frontmatter(path.read_text(encoding="utf-8"))
        uid = str((meta or {}).get("uid") or "").strip() or path.stem
        bodies[uid] = body
    return bodies


def read_page_types(wiki_root: Path) -> dict[str, str]:
    """Read *wiki_root* into ``{uid: type}`` -- the non-person test's input."""
    types: dict[str, str] = {}
    if not wiki_root.is_dir():
        return types
    for path in sorted(wiki_root.glob("*.md")):
        if path.name.startswith("_"):
            continue
        meta, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
        uid = str((meta or {}).get("uid") or "").strip() or path.stem
        types[uid] = str((meta or {}).get("type") or "")
    return types


# ---------------------------------------------------------------------------
# Duplicated prose (case E)
# ---------------------------------------------------------------------------

#: Below this length a repeated fragment is a heading, a list marker or a
#: stock phrase, not a restated fact. Sized to exclude "## Notes" and a bare
#: name while admitting any real sentence.
_MIN_SENTENCE_CHARS = 30

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|\n+")


def duplicated_sentences(body: str) -> list[str]:
    """Sentences appearing more than once in *body* (case E's check).

    "Restated a fact the page already carries, without duplicating the
    sentence" is a property of the RESULT, so it is read off the after-body
    alone rather than as a delta: a page that already said something and now
    says it twice is the failure, however the second copy got there.
    Footnote markers are stripped before comparison so ``X.`` and ``X.[^2]``
    count as the same sentence -- appending a marker to a restated sentence
    is precisely the near-miss this check must still catch.
    """
    seen: dict[str, int] = {}
    for chunk in _SENTENCE_SPLIT_RE.split(body):
        text = _FOOTNOTE_MARKER_RE.sub("", chunk).strip().strip("-*# ").strip()
        if len(text) < _MIN_SENTENCE_CHARS:
            continue
        seen[text] = seen.get(text, 0) + 1
    return sorted(text for text, count in seen.items() if count > 1)


# ---------------------------------------------------------------------------
# Tier attribution: the tier-0 person consult
# ---------------------------------------------------------------------------


def tier0_person_matches(result: Any, call_models: Sequence[str]) -> int:
    """How many pages the DETERMINISTIC tier-0 person consult claimed.

    :func:`~tests.evals.attachment.attribute_tier`'s ``matched`` argument is
    fed from ``ProcessingResult.matched``, which is set immediately after
    ``tiers.tier1_programmatic_match`` runs. The person-registry consult
    early-returns BEFORE that line (``librarian.process_one``), so on the
    shipped path ``result.matched`` is 0 for every case in this layer and
    ``attribute_tier`` would report ``decided_by="none"`` for a decision that
    was very much made -- deterministically, at tier 0. AC4 asks for the tier
    that decided; "none" would be a wrong answer, not a missing one.

    OBSERVED, never declared, on the same rule :class:`~tests.evals.
    attachment.TierAttribution` states: a tier-0 claim is exactly "pages were
    updated and NO model was called". One model call means some later tier
    ran, so this returns 0 and the ordinary classify/write attribution takes
    over unmodified.
    """
    if call_models:
        return 0
    return len(getattr(result, "updated", ()) or [])


def deterministic_matched(result: Any, call_models: Sequence[str]) -> int:
    """``matched=`` for :func:`attribute_tier`, tier-0 consult included."""
    return max(int(getattr(result, "matched", 0) or 0), tier0_person_matches(result, call_models))


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PersonHintObservation:
    """One case's graded observation (AC4).

    Carries the file-level ``decided_by`` label alongside EVERY graded person
    page's outcome, because neither alone answers the layer's question: the
    tier label says what it cost, the outcomes say what it did.
    """

    case_id: str
    attribution: TierAttribution
    person_outcomes: Mapping[str, str]
    non_person_pointer_uids: frozenset[str]

    def describe(self) -> str:
        outcomes = " ".join(
            f"{uid}={self.person_outcomes[uid]}" for uid in sorted(self.person_outcomes)
        )
        return (
            f"decided_by={self.attribution.decided_by} "
            f"person_outcomes[{outcomes}] "
            f"non_person_pointers={sorted(self.non_person_pointer_uids)}"
        )


def non_person_pointer_uids(
    *,
    delta: WikiDelta,
    before_bodies: Mapping[str, str],
    after_bodies: Mapping[str, str],
    page_types: Mapping[str, str],
    raw_ref: str,
) -> frozenset[str]:
    """Non-person pages created or changed carrying a pointer to *raw_ref*.

    Two ways a page can point at the file, both accepted: a
    ``source_ref``/``sources`` entry it GAINED (the mint path, and the
    provenance path on an update), or the ref appearing in the body text that
    was added (the footnote path). Reading only frontmatter would miss a
    correctly-cited body claim; reading only the body would miss a minted
    ``type: source`` page, whose whole pointer is its frontmatter.
    """
    hits: set[str] = set()
    candidates = set(delta.minted) | set(delta.touched_uids) | set(delta.gained_source_refs)
    for uid in candidates:
        if page_types.get(uid, "") == PERSON_TYPE:
            continue
        if raw_ref and raw_ref in (delta.gained_source_refs.get(uid) or frozenset()):
            hits.add(uid)
            continue
        after = after_bodies.get(uid, "")
        before = before_bodies.get(uid, "")
        if raw_ref and raw_ref in after and raw_ref not in before:
            hits.add(uid)
    return frozenset(hits)


def score_case(
    case: Mapping[str, Any],
    delta: WikiDelta,
    person_outcomes: Mapping[str, str],
    *,
    pointer_uids: frozenset[str] = frozenset(),
    duplicated: Mapping[str, Sequence[str]] | None = None,
) -> tuple[bool, str]:
    """Score one person-hint case. Supported ``expected`` keys:

    ``person_outcomes``
        ``{uid: outcome}`` or ``{uid: [outcome, ...]}``. Every listed uid must
        be among the observed outcomes and must carry one of the listed
        shapes. A uid the case does not list is still checked for
        ``notes_bullet`` (below) -- silence is not permission.
    ``require_non_person_pointer``
        AC3's compile requirement: at least one non-person page must have been
        created or changed with a pointer to the raw ref. A run in which the
        only change is on person pages fails.
    ``non_person_pointer_uids``
        Scopes the requirement above to named pages -- every uid listed must
        be among *pointer_uids*. Where the case knows WHICH page the file
        belongs on (case A's project, case D's supplier), naming it is
        strictly stronger than "something non-person moved".
    ``forbid_duplicate_sentence``
        Case E: these pages must carry no sentence twice.

    ``notes_bullet`` is a failure on ANY person page, listed or not, and is
    reported first. It is the shipped tier-0 shape, and an expectation set
    that merely omitted a page must not let it through unremarked.
    """
    expected = dict(case.get("expected") or {})
    reasons: list[str] = []

    bulleted = sorted(
        uid for uid, outcome in person_outcomes.items() if outcome == OUTCOME_NOTES_BULLET
    )
    if bulleted:
        reasons.append(
            f"dated Notes bullet pasted onto {bulleted} (shipped tier-0 shape — "
            "an excerpt of a raw file is not a claim the page makes)"
        )

    wanted = dict(expected.get("person_outcomes") or {})
    for uid, want in wanted.items():
        allowed = [want] if isinstance(want, str) else list(want)
        if uid not in person_outcomes:
            reasons.append(f"{uid}: no page observed (expected {allowed})")
            continue
        got = person_outcomes[uid]
        if got not in allowed:
            reasons.append(f"{uid}: {got} (expected {'|'.join(allowed)})")

    uncited = sorted(
        uid for uid, outcome in person_outcomes.items() if outcome == OUTCOME_UNCITED_CHANGE
    )
    if uncited:
        reasons.append(f"body changed with no citation to the raw ref on {uncited}")

    named = list(expected.get("non_person_pointer_uids") or [])
    if named:
        missing = [uid for uid in named if uid not in pointer_uids]
        if missing:
            reasons.append(f"no pointer to the raw ref on {missing} (AC3 compile requirement)")
    elif expected.get("require_non_person_pointer"):
        if not pointer_uids:
            reasons.append(
                "no non-person page was created or changed with a pointer to the raw ref "
                "(AC3: a run whose only change is on person pages fails)"
            )

    dup_scope = list(expected.get("forbid_duplicate_sentence") or [])
    for uid in dup_scope:
        dupes = list((duplicated or {}).get(uid) or [])
        if dupes:
            reasons.append(f"{uid}: sentence duplicated on the page ({dupes[0][:60]!r})")

    if reasons:
        return False, "; ".join(reasons)
    return True, "ok"


# ---------------------------------------------------------------------------
# Case-spec validation (grading-test input, AC5's shape half)
# ---------------------------------------------------------------------------


def validate_case_spec(cases: Sequence[Mapping[str, Any]], known_uids: Sequence[str]) -> list[str]:
    """Return a list of problems with *cases*, empty when the spec is sound.

    A golden set whose expectations name a uid the wiki does not carry grades
    nothing and says so in no way a reader would notice: the page is simply
    absent from the observed outcomes, the case fails, and the failure reads
    as a librarian result rather than a typo. Checked as DATA rather than
    asserted case by case, so a sixth case added later is covered the day it
    lands.
    """
    problems: list[str] = []
    known = set(known_uids)
    seen_ids: set[str] = set()
    for case in cases:
        cid = str(case.get("id") or "")
        if not cid:
            problems.append("a case has no id")
            continue
        if cid in seen_ids:
            problems.append(f"{cid}: duplicate case id")
        seen_ids.add(cid)
        expected = dict(case.get("expected") or {})
        wanted = dict(expected.get("person_outcomes") or {})
        if not wanted:
            problems.append(f"{cid}: names no expected person outcome")
        for uid, want in wanted.items():
            if uid not in known:
                problems.append(f"{cid}: expects {uid}, which is not a page in the case wiki")
            allowed = [want] if isinstance(want, str) else list(want)
            for outcome in allowed:
                if outcome not in GRADED_OUTCOMES:
                    problems.append(
                        f"{cid}: {uid} expects {outcome!r}, not one of {GRADED_OUTCOMES}"
                    )
        for uid in list(expected.get("non_person_pointer_uids") or []):
            if uid not in known:
                problems.append(f"{cid}: pointer target {uid} is not a page in the case wiki")
        for uid in list(expected.get("forbid_duplicate_sentence") or []):
            if uid not in known:
                problems.append(
                    f"{cid}: duplicate-check target {uid} is not a page in the case wiki"
                )
    return problems
