# SPDX-License-Identifier: Apache-2.0
"""``athenaeum measure shadow-parity`` — issue athenaeum#1333.

Builds (but does not yet run against the live corpus — that is issue
athenaeum#1258) a harness that runs the C4 contradiction detector
(:func:`athenaeum.contradictions.detect_contradictions`, N-ary, one call per
cluster) and the cluster-domain comparator
(:func:`athenaeum.cluster_comparator.run_cluster_comparator`, pairwise) over
the SAME cluster input, and reports:

- a verdict AGREEMENT MATRIX between the two lanes' verdict spaces, and
- the comparator-call-to-detector-call MULTIPLIER
  (:func:`athenaeum.cluster_comparator.planned_pair_count` summed per
  cluster, divided by the detector's one-call-per-cluster count).

This is athenaeum#1256's gate: C4 is not retired here, and this module
**deletes nothing** — it only measures. See the athenaeum#715 phase-4 plan
(issue athenaeum#715) for the retirement sequencing this feeds.

**The comparator is forced ON for this harness, deliberately** (see
:func:`_with_comparator_forced_on`). :func:`athenaeum.config.resolve_comparator_enabled`
defaults OFF everywhere else in the codebase; a measurement harness that
respects that default would silently measure zero comparator calls, which
is not "the comparator is safe to skip" but "the harness never ran it" —
exactly the failure mode this issue exists to avoid. The override is
applied to a COPY of the caller's config (never mutated in place) so a
caller that reuses its own config dict after calling this module is
unaffected.

**Two independent client seams** (``detector_client`` / ``comparator_client``
on :func:`run_shadow_parity`) rather than one shared client: the two lanes
are independently stubbable in tests (see ``tests/test_shadow_parity.py``),
and athenaeum#1258's live run may want to route them through different
backends (e.g. replaying the detector from a recorded fixture while the
comparator runs live) without this module's signature changing.

**Two disjoint verdict spaces, recast onto one another.** The detector
returns a conflict TYPE (or "not detected"); the comparator returns one of
five OUTCOME verdicts. Neither space is a subset of the other, so this
module defines the recast explicitly and names it as a judgement, not a
fact — see :data:`DETECTOR_VERDICTS`, :data:`COMPARATOR_VERDICTS`,
:func:`roll_up_comparator_verdict` (an equally opinionated severity
ordering — the comparator is pairwise and the detector is N-ary, so N
pairwise verdicts must collapse to ONE cluster-level verdict before the two
spaces are even comparable), :func:`classify_agreement`, and
:data:`EXPECTED_COMPARATOR_VERDICTS`'s own docstrings for the specific
calls made and why. athenaeum#1258's live-corpus report is where those
calls get scrutinised against real data; this issue only has to make the
recast machinery correct and legible.

Reused, not reinvented, per this issue's own instruction:
:func:`athenaeum.contradictions.detect_contradictions`,
:func:`athenaeum.cluster_comparator.run_cluster_comparator` /
:func:`~athenaeum.cluster_comparator.planned_pair_count` /
:func:`~athenaeum.cluster_comparator.candidate_pairs` /
:func:`~athenaeum.cluster_comparator.page_from_auto_memory_file`,
:func:`athenaeum.config.resolve_comparator_enabled` /
:func:`~athenaeum.config.resolve_model`,
:class:`athenaeum.models.TokenUsage` /
:func:`~athenaeum.models.estimate_prompt_tokens`, and
:mod:`athenaeum.shadow_linkage`'s provenance-stamping helpers
(:func:`~athenaeum.shadow_linkage._get_version`,
:func:`~athenaeum.shadow_linkage._get_git_sha`,
:func:`~athenaeum.shadow_linkage._now_iso`) — mirrored here rather than
reimplemented, this module's own :func:`_corpus_digest_for_cases` follows
the SAME shape as :func:`athenaeum.shadow_linkage._corpus_digest` but over
this module's own :class:`ParityCase` corpus rather than a live
:class:`~athenaeum.models.AutoMemoryFile` population.

**Layering:** L4 domain/pipeline. Imports :mod:`athenaeum.cluster_comparator`
and :mod:`athenaeum.comparator` (both L4, neither imports this module back)
plus :mod:`athenaeum.contradictions` (L3), :mod:`athenaeum.shadow_linkage`
(L4), :mod:`athenaeum.config` (L2), :mod:`athenaeum.models` (L1), and
:mod:`athenaeum.verdicts` (L2). No cycle: nothing this module imports
imports it back.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import yaml

from athenaeum import comparator as comparator_mod
from athenaeum import contradictions
from athenaeum.cluster_comparator import (
    ClusterComparatorResult,
    candidate_pairs,
    page_from_auto_memory_file,
    planned_pair_count,
    run_cluster_comparator,
)
from athenaeum.comparator import CompareOutcome
from athenaeum.config import DEFAULT_CLASSIFY_MODEL, resolve_comparator_enabled, resolve_model
from athenaeum.models import (
    AutoMemoryFile,
    ConflictType,
    ContradictionResult,
    TokenUsage,
    estimate_prompt_tokens,
)
from athenaeum.provider import resolve_max_tokens
from athenaeum.shadow_linkage import _get_git_sha, _get_version, _now_iso
from athenaeum.verdicts import VERDICT_VALUES

if TYPE_CHECKING:
    from athenaeum.provider import LLMBackend

#: Directory the dated report lands under by default (``measurements/`` at
#: the repo root, mirroring the ``docs/memory-model-measurements.md``
#: convention the other ``measure`` subcommands use, but as one dated file
#: per run rather than an append-only shared doc — athenaeum#1258's live runs
#: are each their own artifact, not a rolling snapshot).
DEFAULT_MEASUREMENTS_DIR = Path("measurements")


# ---------------------------------------------------------------------------
# Corpus loading
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParityMember:
    """One cluster member as loaded from a corpus case's ``members:`` list.

    Mirrors the ``members:`` entry shape both
    ``tests/evals/data/detector/cases.yaml`` and
    ``tests/evals/data/resolver/cases.yaml`` already share
    (``filename``/``body``/``frontmatter``) — see :func:`load_parity_cases`.
    """

    filename: str
    body: str
    frontmatter: dict[str, Any]


@dataclass(frozen=True)
class DeclaredDetectorVerdict:
    """A hand-authored detector verdict declared in a case's ``detector:`` block.

    Only the RESOLVER corpus (``tests/evals/data/resolver/cases.yaml``)
    carries this block — it stands in for "the Haiku detector already
    flagged this pair", exactly as ``tests/evals/test_resolver_eval.py``'s
    ``_detector_result`` helper already treats it, so a resolver-suite case
    costs the parity harness zero detector calls (see
    :func:`run_shadow_parity`).
    """

    conflict_type: ConflictType | None
    rationale: str
    passages: list[str]


@dataclass(frozen=True)
class ParityCase:
    """One corpus case, source-tagged so a per-item report names which
    eval suite (``detector`` / ``resolver``) it came from."""

    case_id: str
    outcome_class: str
    members: tuple[ParityMember, ...]
    declared_detector: DeclaredDetectorVerdict | None
    source: str


def load_parity_cases(path: Path, *, source: str = "") -> list[ParityCase]:
    """Parse a corpus YAML in the eval-case shape into :class:`ParityCase` records.

    Both ``tests/evals/data/detector/cases.yaml`` and
    ``tests/evals/data/resolver/cases.yaml`` share this shape (see this
    module's docstring); unknown top-level or ``members:``-entry keys are
    ignored rather than rejected, so a future eval-suite addition does not
    need a matching change here. ``source`` defaults to ``path.parent.name``
    (``"detector"`` / ``"resolver"`` for the two committed corpora) — pass it
    explicitly only when loading from a path whose parent directory name
    would be misleading.
    """
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    resolved_source = source or path.parent.name

    cases: list[ParityCase] = []
    for entry in raw:
        members = tuple(
            ParityMember(
                filename=str(spec["filename"]),
                body=str(spec["body"]),
                frontmatter=dict(spec.get("frontmatter") or {}),
            )
            for spec in entry.get("members", [])
        )
        declared: DeclaredDetectorVerdict | None = None
        det_raw = entry.get("detector")
        if det_raw:
            conflict_type_raw = det_raw.get("conflict_type")
            conflict_type: ConflictType | None = (
                conflict_type_raw
                if conflict_type_raw in ("factual", "prescriptive", "stance")
                else None
            )
            declared = DeclaredDetectorVerdict(
                conflict_type=conflict_type,
                rationale=str(det_raw.get("rationale", "")),
                passages=[str(p) for p in (det_raw.get("passages") or [])],
            )
        cases.append(
            ParityCase(
                case_id=str(entry["id"]),
                outcome_class=str(entry["outcome_class"]),
                members=members,
                declared_detector=declared,
                source=resolved_source,
            )
        )
    return cases


def materialise_members(case: ParityCase, dest_dir: Path) -> list[AutoMemoryFile]:
    """Write *case*'s members to disk under *dest_dir* and return the
    resolved :class:`~athenaeum.models.AutoMemoryFile` list.

    This is the ONE copy of the eval suites' ``_materialise_members`` shape
    (``tests/evals/test_detector_eval.py:44`` and its
    ``tests/evals/test_resolver_eval.py`` twin) promoted into shipped code —
    those two private test helpers stay exactly as they are (out of this
    issue's scope to refactor); this function is what the harness itself
    calls. Both the detector (:func:`athenaeum.contradictions._build_user_message`)
    and the comparator (:func:`athenaeum.cluster_comparator.page_from_auto_memory_file`)
    re-read the on-disk body + frontmatter, so the fixture must round-trip
    through the real intake frontmatter shape (``---\\n<k: v>\\n---\\n<body>\\n``)
    rather than the dataclass alone.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    members: list[AutoMemoryFile] = []
    for spec in case.members:
        fm = spec.frontmatter
        fm_lines = ["---"]
        for key, value in fm.items():
            fm_lines.append(f"{key}: {value}")
        fm_lines.append("---")
        body = spec.body.rstrip()
        path = dest_dir / spec.filename
        path.write_text("\n".join(fm_lines) + "\n" + body + "\n", encoding="utf-8")
        members.append(
            AutoMemoryFile(
                path=path,
                origin_scope=dest_dir.name,
                memory_type=str(fm.get("type", "feedback")),
                name=str(fm.get("name", spec.filename)),
                source_type=str(fm.get("source_type", "inferred")),
                source_ref=str(fm.get("source_ref", "")),
                valid_from=str(fm.get("valid_from", "")),
                valid_until=str(fm.get("valid_until", "")),
            )
        )
    return members


def _case_scope_dir(workdir: Path, case: ParityCase) -> Path:
    """The on-disk materialisation directory for *case*, under *workdir*.

    ``workdir / case.source / f"scope-{case.case_id}"`` — the LEAF name,
    ``scope-<case_id>``, is not an arbitrary choice: it is the SAME
    scope-directory convention ``tests/evals/test_detector_eval.py`` /
    ``tests/evals/test_resolver_eval.py`` already use (``scope_dir = tmp_path
    / f"scope-{case['id']}"``), which is what the recorded fixtures under
    ``tests/fixtures/recorded/detector/`` were captured against.
    :meth:`~athenaeum.models.AutoMemoryFile.origin_scope` becomes this
    directory's NAME (see :func:`materialise_members`), and ``origin_scope``
    is PROMPT-VISIBLE — :func:`athenaeum.contradictions._member_ref` renders
    it into the detector's user message as ``f"{origin_scope}/{filename}"``
    — so it is part of what the recorded fixture's prompt-hash staleness
    contract checks (``tests.evals.harness.replay_client``). A leaf name
    that diverges from the recording convention manufactures a
    ``FixtureStaleError`` that has nothing to do with real prompt drift; one
    that matches lets a caller (e.g. ``tests/evals/test_shadow_parity_recast.py``,
    issue athenaeum#1333 AC4) replay the real recorded fixtures through this
    harness's OWN materialisation, byte-for-byte.

    The ``case.source`` PARENT directory is what actually needs to be
    unique per case — it keeps two suites that happen to share a case id
    from colliding on disk — so it is kept as a path segment rather than
    folded into the leaf name.

    Factored into one helper so :func:`project_shadow_parity` and
    :func:`run_shadow_parity` (which must materialise the SAME case
    identically, or their call counts and cost figures could diverge) share
    one implementation rather than two copies that could drift apart.
    """
    return workdir / case.source / f"scope-{case.case_id}"


# ---------------------------------------------------------------------------
# Verdict spaces + the recast between them
# ---------------------------------------------------------------------------

#: The detector's verdict space, recast from :class:`~athenaeum.models.ContradictionResult`
#: by :func:`detector_verdict_from_result`. ``"not-detected"`` and the three
#: named conflict types (``factual``/``prescriptive``/``stance``) are the
#: detector's own vocabulary (see ``athenaeum.contradictions._DETECT_SYSTEM``);
#: ``"detected-untyped"`` covers a ``detected=True`` result whose
#: ``conflict_type`` is unset or unrecognized (defensive — the parser
#: already rejects an invalid type down to ``detected=False``, but a future
#: schema change should not silently misclassify here); ``"unavailable"``
#: is a genuinely absent answer (an ``incomplete`` fail-open degrade), never
#: folded into ``"not-detected"``.
DETECTOR_VERDICTS: tuple[str, ...] = (
    "not-detected",
    "factual",
    "prescriptive",
    "stance",
    "detected-untyped",
    "unavailable",
)

#: The comparator's verdict space: the five ledger verdicts
#: (:data:`athenaeum.verdicts.VERDICT_VALUES`) plus ``"no-decision"`` for
#: :attr:`athenaeum.comparator.CompareOutcome.verdict` being ``None`` (Gate 2
#: unavailable) or, after :func:`roll_up_comparator_verdict`'s roll-up, a
#: cluster with zero pairs (a singleton).
COMPARATOR_VERDICTS: tuple[str, ...] = VERDICT_VALUES + ("no-decision",)


def detector_verdict_from_result(result: ContradictionResult) -> str:
    """Map one :class:`~athenaeum.models.ContradictionResult` onto :data:`DETECTOR_VERDICTS`.

    Precedence (checked in this order): ``incomplete=True`` OR
    ``rationale == "llm-unavailable"`` -> ``"unavailable"``. Both are
    genuinely ABSENT answers, not "no contradiction found": ``incomplete``
    is the fail-open degrade after exhausted retries (see
    :func:`athenaeum.contradictions.detect_contradictions`'s ``incomplete``
    contract); ``rationale == "llm-unavailable"`` is the literal
    :func:`~athenaeum.contradictions.detect_contradictions` returns for
    ``client is None`` (no key configured) AND for a non-transient call
    failure — neither of those sets ``incomplete``, so checking
    ``incomplete`` alone missed them (QA finding, live-run repro: a
    ``client=None`` run reported ``detected=False`` for every cluster,
    which this function used to map straight to ``"not-detected"`` — a
    lane that never ran rendering as a genuine "no contradiction" finding,
    scored `agree` against the comparator by :func:`classify_agreement`).
    Everything else: ``detected=False`` -> ``"not-detected"`` (this
    correctly still covers ``rationale == "singleton"`` — a one-member
    cluster genuinely cannot contradict itself; that is a structural fact,
    not a degradation, so it must NOT be swept into ``"unavailable"``);
    else a known ``conflict_type`` -> that type verbatim; else
    ``"detected-untyped"``.
    """
    if result.incomplete or result.rationale == "llm-unavailable":
        return "unavailable"
    if not result.detected:
        return "not-detected"
    if result.conflict_type in ("factual", "prescriptive", "stance"):
        return str(result.conflict_type)
    return "detected-untyped"


#: Cluster-level comparator verdict precedence, most to least severe —
#: see :func:`roll_up_comparator_verdict`.
_COMPARATOR_PRECEDENCE: tuple[str, ...] = (
    "contradiction",
    "underdetermined",
    "specialization",
    "duplicate",
    "distinct",
)


def roll_up_comparator_verdict(outcomes: Sequence[CompareOutcome]) -> str:
    """Collapse N pairwise :class:`~athenaeum.comparator.CompareOutcome` verdicts
    into ONE cluster-level verdict, so the comparator (pairwise) and the
    detector (N-ary, one verdict per cluster) land in comparable units.

    Precedence, most-to-least severe: ``contradiction > underdetermined >
    specialization > duplicate > distinct``. The detector emits exactly one
    verdict per cluster; the comparator emits one per PAIR within a cluster.
    Any single pair finding a contradiction makes the cluster contain a
    contradiction, so ``contradiction`` outranks everything else regardless
    of how many other pairs came back ``distinct``. The remaining order
    mirrors :mod:`athenaeum.comparator`'s own verdict severity (an
    unresolved dimension is more urgent than a resolved specialization,
    which is more urgent than a bare duplicate or an unrelated distinct
    pair).

    ``"no-decision"`` is returned ONLY when no pair produced a non-``None``
    verdict — this includes the zero-pair singleton case (``outcomes`` is
    empty) as well as a cluster whose every pair hit an unavailable Gate 2.
    A ``None`` verdict from any individual pair is simply excluded from the
    precedence scan, never treated as its own outranking value.
    """
    present = {o.verdict for o in outcomes if o.verdict is not None}
    for candidate in _COMPARATOR_PRECEDENCE:
        if candidate in present:
            return candidate
    return "no-decision"


def _comparator_calls_issued(outcomes: Sequence[CompareOutcome]) -> int:
    """Count pairs whose Gate 2 (:func:`athenaeum.comparator.content_relation`)
    LLM call was actually DISPATCHED — not merely "processed by the driver"
    (QA finding B on athenaeum#1333: the two lanes' call counts must mean the
    same thing, or the measured multiplier is apples-to-oranges against the
    detector's ``detector_calls``, which already counts calls actually
    issued).

    Gate 1's typed separator-dimension check can resolve a pair to DISTINCT
    with ZERO model spend
    (:attr:`~athenaeum.comparator.CompareOutcome.comparator_version` ==
    :data:`athenaeum.comparator.COMPARATOR_VERSION_GATE1` — see
    :func:`athenaeum.comparator.compare_pages`'s early ``disjoint_dims``
    return). Every OTHER outcome means
    :func:`~athenaeum.comparator.content_relation` was called: a real
    verdict via Gate 2 (duplicate/specialization/contradiction/coexist-
    distinct), or ``verdict=None`` ("no decision") after Gate 2 was reached
    but the call failed or the response could not be parsed — both of the
    latter still represent a DISPATCHED request, not a skipped one.

    This count is only meaningful once *comparator_client* is guaranteed
    non-``None`` (:func:`run_shadow_parity`'s missing-client preflight):
    a ``None`` client short-circuits ``content_relation`` BEFORE any
    dispatch, landing on the exact same comparator-version-unset shape as
    a genuine post-dispatch failure, which this function cannot tell apart
    from the outside.
    """
    return sum(
        1 for o in outcomes if o.comparator_version != comparator_mod.COMPARATOR_VERSION_GATE1
    )


def classify_agreement(detector_verdict: str, comparator_verdict: str) -> str:
    """Classify one (detector_verdict, comparator_verdict) pair as agreement.

    Total over the full cross product of :data:`DETECTOR_VERDICTS` x
    :data:`COMPARATOR_VERDICTS` — always returns one of ``"agree"`` /
    ``"disagree"`` / ``"inconclusive"``, never raises.

    ``"inconclusive"`` when either side reached no real answer:
    ``detector_verdict == "unavailable"``, or
    ``comparator_verdict in {"no-decision", "underdetermined"}``. These are
    "no verdict reached" states, not wrong answers — scoring them as
    disagreement would understate parity by counting an absent answer as an
    incorrect one.

    Otherwise: the detector's ``"not-detected"`` agrees with the
    comparator's non-conflict outcomes (``distinct``/``duplicate``/
    ``specialization`` — none of these assert a contradiction); any DETECTED
    detector verdict (``factual``/``prescriptive``/``stance``/
    ``detected-untyped``) agrees only with the comparator's
    ``contradiction``. Every other combination disagrees.
    """
    if detector_verdict == "unavailable" or comparator_verdict in (
        "no-decision",
        "underdetermined",
    ):
        return "inconclusive"
    if detector_verdict == "not-detected":
        if comparator_verdict in ("distinct", "duplicate", "specialization"):
            return "agree"
        return "disagree"
    # Any detected verdict (factual/prescriptive/stance/detected-untyped).
    if comparator_verdict == "contradiction":
        return "agree"
    return "disagree"


#: Recast of the eval suites' ``outcome_class`` (``pass``/``contradict``/
#: ``escalate``/``merge``) onto the comparator's verdict space — used by
#: :func:`comparator_decided_correctly` to score the comparator against the
#: golden set's own ground truth (a SEPARATE question from
#: :func:`classify_agreement`, which only compares the two LANES to each
#: other, not either lane to ground truth).
#:
#: **This mapping is a JUDGEMENT, not a fact.** The golden set's classes were
#: authored against the DETECTOR's action taxonomy (``pass`` = no conflict,
#: ``contradict`` = a decidable conflict, ``escalate`` = an undated
#: mutually-exclusive fact, ``merge`` = a refinement/exception pair) — recasting
#: them onto the comparator's five verdicts requires a call about which
#: comparator outcomes count as "got it right" for each class, and that call
#: is made here, once, rather than at every scoring site. athenaeum#1258's
#: live-corpus report is where this mapping gets scrutinised against real
#: data; this issue only needs the recast machinery to be correct and
#: legible.
EXPECTED_COMPARATOR_VERDICTS: dict[str, frozenset[str]] = {
    "pass": frozenset({"distinct", "duplicate", "specialization"}),
    "contradict": frozenset({"contradiction"}),
    "escalate": frozenset({"underdetermined", "contradiction"}),
    "merge": frozenset({"duplicate", "specialization"}),
}


def comparator_decided_correctly(outcome_class: str, comparator_verdict: str) -> bool | None:
    """Score one comparator verdict against the golden set's ``outcome_class``.

    Returns ``None`` ("not scored") for an ``outcome_class`` outside
    :data:`EXPECTED_COMPARATOR_VERDICTS` or for a ``comparator_verdict`` of
    ``"no-decision"`` — neither an unknown class nor an absent answer can be
    marked right or wrong. Otherwise ``True`` iff ``comparator_verdict`` is
    one of the class's expected verdicts.
    """
    expected = EXPECTED_COMPARATOR_VERDICTS.get(outcome_class)
    if expected is None or comparator_verdict == "no-decision":
        return None
    return comparator_verdict in expected


# ---------------------------------------------------------------------------
# Agreement matrix + per-item report rows
# ---------------------------------------------------------------------------


@dataclass
class AgreementMatrix:
    """Cross-tabulation of (detector_verdict, comparator_verdict) pairs.

    ``inconclusive_count`` is reported ALONGSIDE ``agree_count`` /
    ``disagree_count`` — never folded into either, so a reader can see how
    much of the corpus reached no real answer on either lane (see
    :func:`classify_agreement`).
    """

    counts: dict[tuple[str, str], int] = field(default_factory=dict)

    def add(self, detector_verdict: str, comparator_verdict: str) -> None:
        key = (detector_verdict, comparator_verdict)
        self.counts[key] = self.counts.get(key, 0) + 1

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    @property
    def agree_count(self) -> int:
        return sum(
            n for (d, c), n in self.counts.items() if classify_agreement(d, c) == "agree"
        )

    @property
    def disagree_count(self) -> int:
        return sum(
            n for (d, c), n in self.counts.items() if classify_agreement(d, c) == "disagree"
        )

    @property
    def inconclusive_count(self) -> int:
        return sum(
            n for (d, c), n in self.counts.items() if classify_agreement(d, c) == "inconclusive"
        )

    @property
    def agreement_rate(self) -> float | None:
        """``agree / (agree + disagree)``; ``None`` when that denominator is 0
        (every item was inconclusive, or the matrix is empty)."""
        denom = self.agree_count + self.disagree_count
        if denom == 0:
            return None
        return self.agree_count / denom

    def to_dict(self) -> dict[str, Any]:
        return {
            "counts": {f"{d}|{c}": n for (d, c), n in self.counts.items()},
            "total": self.total,
            "agree_count": self.agree_count,
            "disagree_count": self.disagree_count,
            "inconclusive_count": self.inconclusive_count,
            "agreement_rate": self.agreement_rate,
        }


@dataclass
class ParityItem:
    """One case's result: both lanes' verdicts, the agreement classification,
    and the raw pairwise comparator verdicts behind the roll-up.

    ``detector_calls`` and ``comparator_calls`` share ONE definition:
    calls actually DISPATCHED to a client (QA finding B on athenaeum#1333).
    ``comparator_calls`` is NOT ``len(pair_verdicts)`` — a Gate-1-resolved
    pair appears in ``pair_verdicts`` (it has a real verdict) but costs zero
    dispatches; see :func:`_comparator_calls_issued`.
    """

    case_id: str
    source: str
    outcome_class: str
    detector_verdict: str
    comparator_verdict: str
    pair_verdicts: list[dict[str, Any]] = field(default_factory=list)
    agreement: str = ""
    comparator_correct: bool | None = None
    detector_calls: int = 0
    comparator_calls: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "source": self.source,
            "outcome_class": self.outcome_class,
            "detector_verdict": self.detector_verdict,
            "comparator_verdict": self.comparator_verdict,
            "pair_verdicts": self.pair_verdicts,
            "agreement": self.agreement,
            "comparator_correct": self.comparator_correct,
            "detector_calls": self.detector_calls,
            "comparator_calls": self.comparator_calls,
        }


# ---------------------------------------------------------------------------
# Zero-call cost/call projection
# ---------------------------------------------------------------------------


@dataclass
class ParityProjection:
    """A zero-LLM-call sizing of what a real :func:`run_shadow_parity` run
    over the same cases WOULD cost — see :func:`project_shadow_parity`.

    ``projected_comparator_calls`` is a WORST-CASE pair count
    (:func:`athenaeum.cluster_comparator.planned_pair_count`, pure
    combinatorics) — it does not, and structurally cannot without spending
    a call, predict which pairs Gate 1 would resolve for free. Contrast
    with :attr:`ParityReport.comparator_calls`, the MEASURED count of calls
    actually dispatched; the two coincide exactly when no pair in the run
    is Gate-1-resolved, and the measured figure is allowed to be lower.
    """

    cluster_count: int
    pairable_cluster_count: int
    projected_detector_calls: int
    projected_comparator_calls: int
    projected_multiplier: float | None
    projected_cost_usd_lower: float
    projected_cost_usd_upper: float
    detector_model: str
    comparator_model: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "cluster_count": self.cluster_count,
            "pairable_cluster_count": self.pairable_cluster_count,
            "projected_detector_calls": self.projected_detector_calls,
            "projected_comparator_calls": self.projected_comparator_calls,
            "projected_multiplier": self.projected_multiplier,
            "projected_cost_usd_lower": self.projected_cost_usd_lower,
            "projected_cost_usd_upper": self.projected_cost_usd_upper,
            "detector_model": self.detector_model,
            "comparator_model": self.comparator_model,
        }


def _with_comparator_forced_on(config: dict[str, Any] | None) -> dict[str, Any]:
    """Return a COPY of *config* with ``librarian.comparator_enabled`` forced
    ``True`` (the exact yaml key :func:`athenaeum.config.resolve_comparator_enabled`
    reads — see that function's docstring). Never mutates *config* or any
    dict it contains: only the top-level dict and the ``librarian`` sub-dict
    are shallow-copied before the override key is set, so a caller that
    reuses its own config after calling this module sees it unchanged.
    """
    base = dict(config) if isinstance(config, dict) else {}
    librarian_cfg = dict(base.get("librarian") or {})
    librarian_cfg["comparator_enabled"] = True
    base["librarian"] = librarian_cfg
    return base


def project_shadow_parity(
    cases: list[ParityCase],
    *,
    config: dict[str, Any] | None = None,
    workdir: Path,
) -> ParityProjection:
    """Size a :func:`run_shadow_parity` run over *cases* with ZERO client calls.

    Takes no client argument at all — this is the structural guarantee
    behind "zero paid calls" (``--dry-run``, athenaeum#1333 AC5), not merely a
    convention this function happens to follow.

    Call counts: a cluster projects ONE detector call when it has >= 2
    members AND no :attr:`ParityCase.declared_detector` (mirrors
    :func:`athenaeum.contradictions.detect_contradictions`'s own
    ``len(cluster_members) < 2`` short-circuit and this harness's own
    "declared verdict costs zero calls" rule — see :func:`run_shadow_parity`).
    Comparator calls per cluster are :func:`athenaeum.cluster_comparator.planned_pair_count`
    — pure combinatorics, computed regardless of the comparator gate.

    Cost bounds: for every planned call, this function builds the REAL
    prompt (:func:`athenaeum.contradictions._build_user_message` +
    ``_DETECT_SYSTEM`` for the detector;
    :func:`athenaeum.comparator._build_content_relation_messages` +
    ``_CONTENT_RELATION_SYSTEM`` for the comparator) and sizes it with
    :func:`athenaeum.models.estimate_prompt_tokens` — a documented LOWER
    BOUND on the real token count. The lower cost bound assumes zero output
    tokens; the upper bound assumes each call's full configured
    ``max_tokens`` of output. Both are genuinely BOUNDS, not an expected
    value: because the token estimate under-counts, the true cost of a real
    run is expected to sit somewhere above the lower bound, not below it.
    Pricing goes through :class:`~athenaeum.models.TokenUsage` (two throwaway
    accumulators, one per bound) so projection and a real run's
    ``usage.estimated_cost_usd`` share the exact same arithmetic — this
    function never hand-rolls a second price formula.

    Models are resolved exactly as the real call sites resolve them: the
    detector via :func:`athenaeum.contradictions._get_model`, the comparator
    via :func:`athenaeum.config.resolve_model` with the SAME
    knob/env-var/default triple :func:`athenaeum.comparator.content_relation`
    uses.
    """
    detector_model = contradictions._get_model(config)
    comparator_model = resolve_model(
        comparator_mod._CONTENT_RELATION_MODEL_KNOB,
        comparator_mod._CONTENT_RELATION_ENV_VAR,
        DEFAULT_CLASSIFY_MODEL,
        config,
    )
    detector_max_tokens = resolve_max_tokens(
        "contradiction_detect",
        "ATHENAEUM_CONTRADICTION_DETECT_MAX_TOKENS",
        contradictions._DETECT_MAX_TOKENS,
        config,
    )
    comparator_max_tokens = resolve_max_tokens(
        "comparator_content_relation",
        "ATHENAEUM_COMPARATOR_CONTENT_RELATION_MAX_TOKENS",
        comparator_mod._CONTENT_RELATION_MAX_TOKENS,
        config,
    )

    pairable_cluster_count = 0
    projected_detector_calls = 0
    projected_comparator_calls = 0
    usage_lower = TokenUsage()
    usage_upper = TokenUsage()

    for case in cases:
        dest_dir = _case_scope_dir(workdir, case)
        members = materialise_members(case, dest_dir)

        pair_count = planned_pair_count(members)
        projected_comparator_calls += pair_count
        if pair_count > 0:
            pairable_cluster_count += 1

        if case.declared_detector is None and len(members) >= 2:
            projected_detector_calls += 1
            user_msg = contradictions._build_user_message(members)
            input_tokens = estimate_prompt_tokens(contradictions._DETECT_SYSTEM + "\n" + user_msg)
            usage_lower.add_tokens(input_tokens, 0, model=detector_model, knob="classify")
            usage_upper.add_tokens(
                input_tokens, detector_max_tokens, model=detector_model, knob="classify"
            )

        for member_a, member_b in candidate_pairs(members):
            page_a = page_from_auto_memory_file(member_a)
            page_b = page_from_auto_memory_file(member_b)
            user_msg = comparator_mod._build_content_relation_messages(page_a, page_b)
            input_tokens = estimate_prompt_tokens(
                comparator_mod._CONTENT_RELATION_SYSTEM + "\n" + user_msg
            )
            usage_lower.add_tokens(input_tokens, 0, model=comparator_model, knob="classify")
            usage_upper.add_tokens(
                input_tokens, comparator_max_tokens, model=comparator_model, knob="classify"
            )

    multiplier = (
        projected_comparator_calls / projected_detector_calls
        if projected_detector_calls > 0
        else None
    )

    return ParityProjection(
        cluster_count=len(cases),
        pairable_cluster_count=pairable_cluster_count,
        projected_detector_calls=projected_detector_calls,
        projected_comparator_calls=projected_comparator_calls,
        projected_multiplier=multiplier,
        projected_cost_usd_lower=usage_lower.estimated_cost_usd,
        projected_cost_usd_upper=usage_upper.estimated_cost_usd,
        detector_model=detector_model,
        comparator_model=comparator_model,
    )


# ---------------------------------------------------------------------------
# The real (client-calling) run
# ---------------------------------------------------------------------------


def _corpus_digest_for_cases(cases: Sequence[ParityCase]) -> str:
    """Content-address *cases*: sha256 of sorted ``source/case_id:sha256(members)[:12]``.

    Mirrors :func:`athenaeum.shadow_linkage._corpus_digest`'s shape (per-item
    hash, sorted, then hashed again) over this module's own
    :class:`ParityCase` corpus rather than a live
    :class:`~athenaeum.models.AutoMemoryFile` population — changes whenever a
    case's member content, OR its frontmatter (``valid_from``, ``source_type``,
    ``updated``, etc. all feed the detector/comparator prompts — see
    :func:`athenaeum.contradictions._member_scope_header`), or the case SET
    itself, changes. Frontmatter is serialized as sorted ``key=value`` pairs
    (not dict iteration order) so two semantically-identical frontmatter
    dicts that merely differ in key insertion order still hash equal.
    """
    parts: list[str] = []
    for case in cases:
        member_parts: list[str] = []
        for m in case.members:
            fm_pairs = sorted((str(k), str(v)) for k, v in m.frontmatter.items())
            fm_repr = ",".join(f"{k}={v}" for k, v in fm_pairs)
            member_parts.append(f"{m.filename}[{fm_repr}]:{m.body}")
        # ASCII record separator -- avoids accidental cross-member collision.
        blob = "\x1e".join(member_parts)
        h = hashlib.sha256(blob.encode("utf-8", errors="replace")).hexdigest()[:12]
        parts.append(f"{case.source}/{case.case_id}:{h}")
    parts.sort()
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:16]


@dataclass
class ParityReport:
    """Full shadow-parity measurement: agreement matrix, call multiplier,
    per-item detail, the zero-call projection, and a provenance stamp.

    ``detector_calls`` / ``comparator_calls`` / ``call_multiplier`` are all
    MEASURED figures over calls actually DISPATCHED to a client — the SAME
    definition on both lanes (QA finding B on athenaeum#1333: before this,
    ``comparator_calls`` counted pairs PROCESSED, which includes pairs
    Gate 1 resolved with zero model spend, making the multiplier
    apples-to-oranges against the detector's always-"dispatched" count).
    Contrast with ``projection.projected_comparator_calls``
    (:class:`ParityProjection`), a PRE-RUN, zero-call, worst-case pair
    count (:func:`athenaeum.cluster_comparator.planned_pair_count`) that
    does NOT attempt to predict which pairs Gate 1 would resolve for free
    — the two numbers coincide exactly when no pair in the run is
    Gate-1-resolved, and the measured figure is allowed to be lower
    otherwise. ``call_multiplier`` is ``None`` when ``detector_calls == 0``.
    """

    items: list[ParityItem]
    matrix: AgreementMatrix
    detector_calls: int
    comparator_calls: int
    call_multiplier: float | None
    usage: TokenUsage
    cost_usd: float
    max_usd: float | None
    aborted: bool
    abort_reason: str
    projection: ParityProjection
    athenaeum_version: str
    git_sha: str
    generated: str
    corpus_digest: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "items": [item.to_dict() for item in self.items],
            "matrix": self.matrix.to_dict(),
            "detector_calls": self.detector_calls,
            "comparator_calls": self.comparator_calls,
            "call_multiplier": self.call_multiplier,
            "usage": {
                "input_tokens": self.usage.input_tokens,
                "output_tokens": self.usage.output_tokens,
                "estimated_cost_usd": self.usage.estimated_cost_usd,
            },
            "cost_usd": self.cost_usd,
            "max_usd": self.max_usd,
            "aborted": self.aborted,
            "abort_reason": self.abort_reason,
            "projection": self.projection.to_dict(),
            "athenaeum_version": self.athenaeum_version,
            "git_sha": self.git_sha,
            "generated": self.generated,
            "corpus_digest": self.corpus_digest,
        }


class _CostCeilingExceeded(BaseException):
    """Raised by :class:`_CeilingGuardedMessages` BEFORE dispatching a call,
    once the running spend already exceeds ``--max-usd`` (QA finding 2 on
    issue athenaeum#1333).

    Deliberately inherits from :class:`BaseException`, NOT :class:`Exception`
    — mirrors ``tests.evals.harness.FixtureStaleError`` /
    ``EmptyRecordingError``'s identical reasoning, cited here because this
    module hits the SAME hazard those two guard against:
    :func:`athenaeum.comparator.content_relation` wraps its
    ``client.messages.create`` call in :func:`athenaeum._retry.with_retry`
    inside a ``try: ... except Exception:`` fallback that degrades to
    :attr:`~athenaeum.comparator.ContentRelation.UNAVAILABLE`, and
    :func:`athenaeum.contradictions.detect_contradictions` has the
    equivalent ``detected=False`` fallback. An :class:`Exception` subclass
    raised from inside the wrapped call would be swallowed by either
    fallback and the run would silently continue past the ceiling instead
    of aborting — the exact failure this class exists to prevent.
    """


class _CeilingGuardedMessages:
    """The ``.messages`` facade for :class:`_CeilingGuardedClient`."""

    def __init__(self, inner_messages: Any, usage: TokenUsage, max_usd: float) -> None:
        self._inner_messages = inner_messages
        self._usage = usage
        self._max_usd = max_usd

    def create(self, **params: Any) -> Any:
        if self._usage.estimated_cost_usd > self._max_usd:
            raise _CostCeilingExceeded(
                f"observed spend ${self._usage.estimated_cost_usd:.4f} exceeds "
                f"--max-usd ${self._max_usd:.2f} -- aborting before dispatching "
                "another call"
            )
        return self._inner_messages.create(**params)


class _CeilingGuardedClient:
    """Wraps a real ``LLMBackend`` so every ``.messages.create`` call checks
    the running ``--max-usd`` ceiling BEFORE dispatching, not merely between
    cases (QA finding 2 on issue athenaeum#1333).

    :func:`athenaeum.cluster_comparator.run_cluster_comparator` loops every
    :func:`~athenaeum.cluster_comparator.candidate_pairs` entry with no
    per-call cost hook of its own — a single cluster can plan up to
    ``C(cluster_size_cap, 2)`` pairs (300 at this repo's own
    ``resolve_cluster_size_cap`` default of 25 members), all of which could
    fire inside ONE case before :func:`run_shadow_parity`'s own
    between-CASES check is re-read. Wrapping the client here — rather than
    touching :mod:`athenaeum.cluster_comparator` — checks between every
    CALL instead, for both the detector and comparator lanes.
    """

    def __init__(self, inner: Any, usage: TokenUsage, max_usd: float) -> None:
        self._messages = _CeilingGuardedMessages(inner.messages, usage, max_usd)

    @property
    def messages(self) -> _CeilingGuardedMessages:
        return self._messages


def _wrap_with_ceiling_guard(
    client: "LLMBackend | None", usage: TokenUsage, max_usd: float | None
) -> "LLMBackend | None":
    """Return *client* unchanged when there is no ceiling to guard
    (``max_usd is None``) or no client to wrap (``client is None`` — the
    LLM-unavailable fallback path); otherwise wrap it in
    :class:`_CeilingGuardedClient`."""
    if max_usd is None or client is None:
        return client
    return cast("LLMBackend", _CeilingGuardedClient(client, usage, max_usd))


def _empty_report(
    *, max_usd: float | None, abort_reason: str, projection: ParityProjection, corpus_digest: str
) -> ParityReport:
    return ParityReport(
        items=[],
        matrix=AgreementMatrix(),
        detector_calls=0,
        comparator_calls=0,
        call_multiplier=None,
        usage=TokenUsage(),
        cost_usd=0.0,
        max_usd=max_usd,
        aborted=True,
        abort_reason=abort_reason,
        projection=projection,
        athenaeum_version=_get_version(),
        git_sha=_get_git_sha(),
        generated=_now_iso(),
        corpus_digest=corpus_digest,
    )


def run_shadow_parity(
    cases: list[ParityCase],
    *,
    detector_client: "LLMBackend | None",
    comparator_client: "LLMBackend | None",
    config: dict[str, Any] | None = None,
    max_usd: float | None = None,
    workdir: Path,
) -> ParityReport:
    """Run both lanes over every case in *cases* and report the agreement matrix.

    **Missing-client preflight (QA follow-up on athenaeum#1333 finding 1).**
    Aborts immediately, before anything else, if *detector_client* or
    *comparator_client* is ``None`` — naming which one. A live run
    reproduced this exact failure mode: with no ``ANTHROPIC_API_KEY`` set,
    :func:`athenaeum.provider.build_llm_client` returns ``None`` for both
    lanes, ``detect_contradictions`` degraded every cluster to
    ``detected=False`` ("llm-unavailable"), and the harness reported a
    clean ``agreement_rate: 1.000`` — a parity harness with no model client
    measures nothing, and must never render as a legitimate result.

    Computes :func:`project_shadow_parity` FIRST. When *max_usd* is given and
    the projection's lower cost bound already exceeds it, returns
    immediately with ``aborted=True`` and NO items — a ceiling that can only
    be discovered mid-run is not a ceiling (athenaeum#1333 AC6).

    **Comparator-gate preflight (QA finding 1).** :func:`_with_comparator_forced_on`
    overlays ``librarian.comparator_enabled=True`` onto the effective config,
    but :func:`athenaeum.config.resolve_comparator_enabled` reads the
    ``ATHENAEUM_COMPARATOR_ENABLED`` environment variable FIRST and
    unconditionally — a falsy env value overrides the yaml override this
    module makes. Before running anything, this function re-resolves the
    gate on the effective config and aborts immediately (no items) if it is
    still off: a report whose comparator lane never actually ran would
    otherwise render as a legitimate "zero calls, zero multiplier" result
    indistinguishable from a genuine finding, and this report gates a real
    spend decision (athenaeum#1258) and an irreversible C4 retirement
    (athenaeum#1256). A SECOND belt checks
    :attr:`~athenaeum.cluster_comparator.ClusterComparatorResult.gate_enabled`
    after every single :func:`~athenaeum.cluster_comparator.run_cluster_comparator`
    call, in case the environment changes mid-run.

    Per case, in order: materialise members under
    :func:`_case_scope_dir`'s ``workdir/<source>/scope-<case_id>/`` (the
    ``scope-<case_id>`` leaf matches the recorded-fixture convention, so
    real fixtures replay through this materialisation unchanged — see that
    function's docstring); resolve the detector verdict (a
    declared verdict from ``tests/evals/data/resolver/cases.yaml``-shaped
    cases costs 0 calls; otherwise
    :func:`athenaeum.contradictions.detect_contradictions` runs, which
    itself costs 0 calls for a <2-member cluster); run
    :func:`athenaeum.cluster_comparator.run_cluster_comparator` with the
    comparator gate FORCED on (:func:`_with_comparator_forced_on`); build the
    :class:`ParityItem`; fold both verdicts into the :class:`AgreementMatrix`.
    ONE shared :class:`~athenaeum.models.TokenUsage` instance threads through
    both lanes for every case, so ``usage.estimated_cost_usd`` is the true
    running spend across the whole run.

    **Per-call ceiling, not just per-case (QA finding 2).** Both clients are
    wrapped via :func:`_wrap_with_ceiling_guard`, which checks the running
    ceiling BEFORE every single ``.messages.create`` dispatch — not merely
    between cases — because :func:`~athenaeum.cluster_comparator.run_cluster_comparator`
    loops an entire cluster's candidate pairs (up to 300 at this repo's own
    25-member cluster-size cap) with no cost hook of its own. The wrapper
    raises :class:`_CostCeilingExceeded` (caught here, around each case) once
    already over budget; the ORIGINAL between-cases check below still runs
    too, for the case where the ceiling is crossed exactly at a case
    boundary with no further call to trip the per-call guard.

    After each case, when *max_usd* is given and the running
    ``usage.estimated_cost_usd`` exceeds it, stops and returns the PARTIAL
    report (the items completed so far, ``aborted=True``) rather than
    raising — a lane lost mid-run must still report what it measured.

    Never lets a lane exception abort the whole run silently: both
    :func:`~athenaeum.contradictions.detect_contradictions` and
    :func:`~athenaeum.cluster_comparator.run_cluster_comparator` already
    degrade rather than raise on an LLM failure, so this function adds no
    blanket ``except Exception`` of its own — a genuine bug in either lane
    surfaces as a real traceback, not a silently-partial report. The one
    exception TYPE this function does catch, :class:`_CostCeilingExceeded`,
    is deliberately a :class:`BaseException` for exactly that reason (see
    its docstring) — a blanket ``except Exception`` could never have caught
    it in the first place.
    """
    projection = project_shadow_parity(cases, config=config, workdir=workdir)
    corpus_digest = _corpus_digest_for_cases(cases)

    if detector_client is None or comparator_client is None:
        missing = [
            name
            for name, client in (
                ("detector_client", detector_client),
                ("comparator_client", comparator_client),
            )
            if client is None
        ]
        return _empty_report(
            max_usd=max_usd,
            abort_reason=(
                f"{' and '.join(missing)} is None -- a shadow-parity run with "
                "no model client for one or both lanes measures nothing: "
                "an unavailable detector degrades to detected=False "
                "('llm-unavailable'), which would otherwise be scored as a "
                "genuine 'no contradiction found' rather than an absent "
                "answer, and an unavailable comparator degrades to "
                "verdict=None the same way. Build a real client for BOTH "
                "lanes (see athenaeum.provider.build_llm_client -- this "
                "usually means ANTHROPIC_API_KEY is unset) before running "
                "for real, or use --dry-run for a zero-call projection "
                "instead of a run that would otherwise fabricate a report."
            ),
            projection=projection,
            corpus_digest=corpus_digest,
        )

    if max_usd is not None and projection.projected_cost_usd_lower > max_usd:
        return _empty_report(
            max_usd=max_usd,
            abort_reason=(
                f"projected cost ${projection.projected_cost_usd_lower:.4f} exceeds "
                f"--max-usd ${max_usd:.2f}"
            ),
            projection=projection,
            corpus_digest=corpus_digest,
        )

    effective_config = _with_comparator_forced_on(config)

    if not resolve_comparator_enabled(effective_config):
        return _empty_report(
            max_usd=max_usd,
            abort_reason=(
                "the comparator gate is still OFF after forcing "
                "librarian.comparator_enabled=True in the effective config -- "
                "the ATHENAEUM_COMPARATOR_ENABLED environment variable takes "
                "precedence over that yaml key "
                "(see athenaeum.config.resolve_comparator_enabled) and is set "
                "to a value that resolves to False. Unset ATHENAEUM_COMPARATOR_ENABLED "
                "(or set it to a truthy value: 1/true/yes/on) before running "
                "this harness -- otherwise the comparator lane never runs and "
                "every item would report a fabricated 'no-decision', "
                "indistinguishable from a real zero-agreement finding."
            ),
            projection=projection,
            corpus_digest=corpus_digest,
        )

    usage = TokenUsage()
    guarded_detector_client = _wrap_with_ceiling_guard(detector_client, usage, max_usd)
    guarded_comparator_client = _wrap_with_ceiling_guard(comparator_client, usage, max_usd)

    matrix = AgreementMatrix()
    items: list[ParityItem] = []
    detector_calls = 0
    comparator_calls = 0
    aborted = False
    abort_reason = ""

    for case in cases:
        dest_dir = _case_scope_dir(workdir, case)
        members = materialise_members(case, dest_dir)

        try:
            if case.declared_detector is not None:
                det = case.declared_detector
                result = ContradictionResult(
                    detected=True,
                    conflict_type=det.conflict_type,
                    conflicting_passages=list(det.passages),
                    rationale=det.rationale,
                )
                case_detector_calls = 0
            else:
                result = contradictions.detect_contradictions(
                    members, guarded_detector_client, config=config, usage=usage
                )
                # Mirrors detect_contradictions' own short-circuit conditions
                # (<2 members, or client is None) exactly, rather than guessing
                # from the result alone -- both short-circuits return
                # detected=False without ever dispatching a request.
                case_detector_calls = (
                    1 if (len(members) >= 2 and detector_client is not None) else 0
                )
            detector_verdict = detector_verdict_from_result(result)

            cluster_result: ClusterComparatorResult = run_cluster_comparator(
                members,
                guarded_comparator_client,
                config=effective_config,
                usage=usage,
                cluster_id=f"{case.source}-{case.case_id}",
            )
        except _CostCeilingExceeded as exc:
            aborted = True
            abort_reason = f"{exc} (during case {case.source}/{case.case_id!r})"
            break

        if not cluster_result.gate_enabled:
            # Belt 2 of QA finding 1: this should be unreachable given the
            # preflight check above (the SAME effective_config is passed to
            # every case), but if it ever fires, an empty `outcomes` must
            # never be folded into the matrix as a fabricated "no-decision"
            # -- abort instead.
            aborted = True
            abort_reason = (
                f"comparator gate reported disabled (gate_enabled=False) for "
                f"case {case.source}/{case.case_id!r} -- the comparator lane "
                "did not run for this case even though the preflight check "
                "passed; aborting rather than reporting a fabricated "
                "no-decision result"
            )
            break

        detector_calls += case_detector_calls
        case_comparator_calls = _comparator_calls_issued(
            [outcome for _a, _b, outcome in cluster_result.outcomes]
        )
        comparator_calls += case_comparator_calls
        comparator_verdict = roll_up_comparator_verdict(
            [outcome for _a, _b, outcome in cluster_result.outcomes]
        )

        agreement = classify_agreement(detector_verdict, comparator_verdict)
        correct = comparator_decided_correctly(case.outcome_class, comparator_verdict)

        items.append(
            ParityItem(
                case_id=case.case_id,
                source=case.source,
                outcome_class=case.outcome_class,
                detector_verdict=detector_verdict,
                comparator_verdict=comparator_verdict,
                pair_verdicts=[
                    {"a": id_a, "b": id_b, "verdict": outcome.verdict}
                    for id_a, id_b, outcome in cluster_result.outcomes
                ],
                agreement=agreement,
                comparator_correct=correct,
                detector_calls=case_detector_calls,
                comparator_calls=case_comparator_calls,
            )
        )
        matrix.add(detector_verdict, comparator_verdict)

        if max_usd is not None and usage.estimated_cost_usd > max_usd:
            aborted = True
            abort_reason = (
                f"observed spend ${usage.estimated_cost_usd:.4f} exceeds "
                f"--max-usd ${max_usd:.2f} after case {case.source}/{case.case_id!r}"
            )
            break

    call_multiplier = comparator_calls / detector_calls if detector_calls > 0 else None

    return ParityReport(
        items=items,
        matrix=matrix,
        detector_calls=detector_calls,
        comparator_calls=comparator_calls,
        call_multiplier=call_multiplier,
        usage=usage,
        cost_usd=usage.estimated_cost_usd,
        max_usd=max_usd,
        aborted=aborted,
        abort_reason=abort_reason,
        projection=projection,
        athenaeum_version=_get_version(),
        git_sha=_get_git_sha(),
        generated=_now_iso(),
        corpus_digest=corpus_digest,
    )


# ---------------------------------------------------------------------------
# Rendering + writing
# ---------------------------------------------------------------------------


def render_report(report: ParityReport) -> str:
    """Render *report* as markdown: agreement rate, the agreement matrix
    table, the call multiplier, a per-item table, the projection bounds, and
    a provenance stamp — with a prominent "PARTIAL" banner when
    ``report.aborted``."""
    lines: list[str] = []
    if report.aborted:
        lines.append("> **PARTIAL RUN** — stopped early.")
        lines.append(f"> abort_reason: {report.abort_reason}")
        lines.append("")

    lines.append("# Shadow parity: C4 detector vs cluster comparator (athenaeum#1333)")
    lines.append("")
    lines.append(f"- generated: {report.generated}")
    lines.append(f"- athenaeum_version: {report.athenaeum_version}")
    lines.append(f"- git_sha: {report.git_sha}")
    lines.append(f"- corpus_digest: {report.corpus_digest}")
    lines.append("")

    lines.append("## Agreement")
    lines.append("")
    lines.append(
        "`agreement_rate = agree / (agree + disagree)` -- INCONCLUSIVE items "
        "(either lane reached no real verdict: detector `unavailable`, or "
        "comparator `no-decision`/`underdetermined`) are EXCLUDED from both "
        "the numerator and the denominator, never folded into either side. "
        "A rate computed over a small adjudicated subset describes ONLY that "
        "subset, not the whole corpus -- always read it alongside "
        "`inconclusive`, never in isolation (a corpus that is 90% "
        "inconclusive with 9 of the remaining 10 agreeing still renders "
        "`0.900`, which is NOT \"the lanes agree on 90% of the corpus\")."
    )
    lines.append("")
    agree_n = report.matrix.agree_count
    disagree_n = report.matrix.disagree_count
    inconclusive_n = report.matrix.inconclusive_count
    rate = report.matrix.agreement_rate
    if rate is not None:
        rate_str = (
            f"{rate:.3f} ({agree_n} agree / ({agree_n} agree + {disagree_n} disagree); "
            f"{inconclusive_n} inconclusive item(s) excluded from this rate)"
        )
    else:
        rate_str = (
            f"n/a (0 agree + 0 disagree; {inconclusive_n} inconclusive item(s) -- "
            "nothing was adjudicated)"
        )
    lines.append(f"- agreement_rate: {rate_str}")
    lines.append(f"- agree: {agree_n}")
    lines.append(f"- disagree: {disagree_n}")
    lines.append(f"- inconclusive: {inconclusive_n}")
    lines.append("")

    lines.append("## Call multiplier (comparator calls per detector call)")
    lines.append("")
    lines.append(
        "Both counts below are calls actually DISPATCHED to a client "
        "(never pairs merely processed) -- a pair Gate 1 resolves for free "
        "(zero model spend) does not count as a comparator call. See the "
        "Projection section for the pre-run, zero-call, worst-case pair "
        "count, which this measured figure can legitimately fall below."
    )
    lines.append("")
    mult_str = f"{report.call_multiplier:.3f}" if report.call_multiplier is not None else "n/a"
    lines.append(f"- detector_calls: {report.detector_calls}")
    lines.append(f"- comparator_calls: {report.comparator_calls}")
    lines.append(f"- call_multiplier: {mult_str}")
    lines.append("")

    lines.append("## Agreement matrix (detector_verdict x comparator_verdict)")
    lines.append("")
    lines.append("| detector_verdict | comparator_verdict | count |")
    lines.append("| --- | --- | --- |")
    for (d, c), n in sorted(report.matrix.counts.items()):
        lines.append(f"| {d} | {c} | {n} |")
    lines.append("")

    lines.append("## Per-item results")
    lines.append("")
    lines.append(
        "`decided_correctly` legend: `True` = the comparator verdict is one "
        "of this item's `outcome_class`'s expected verdicts "
        "(`EXPECTED_COMPARATOR_VERDICTS`, a judgement -- see the module "
        "docstring); `False` = it is not; `None` = not scored (an "
        "`outcome_class` outside that mapping, or the comparator reached "
        "`no-decision`)."
    )
    lines.append("")
    lines.append(
        "| source | case_id | outcome_class | detector_verdict | comparator_verdict "
        "| agreement | decided_correctly |"
    )
    lines.append("| --- | --- | --- | --- | --- | --- | --- |")
    for item in report.items:
        lines.append(
            f"| {item.source} | {item.case_id} | {item.outcome_class} | "
            f"{item.detector_verdict} | {item.comparator_verdict} | {item.agreement} | "
            f"{item.comparator_correct} |"
        )
    lines.append("")

    lines.append("## Projection (zero-call dry-run bounds)")
    lines.append("")
    p = report.projection
    lines.append(f"- cluster_count: {p.cluster_count}")
    lines.append(f"- pairable_cluster_count: {p.pairable_cluster_count}")
    lines.append(f"- projected_detector_calls: {p.projected_detector_calls}")
    lines.append(f"- projected_comparator_calls: {p.projected_comparator_calls}")
    proj_mult_str = (
        f"{p.projected_multiplier:.3f}" if p.projected_multiplier is not None else "n/a"
    )
    lines.append(f"- projected_multiplier: {proj_mult_str}")
    lines.append(
        f"- projected_cost_usd: ${p.projected_cost_usd_lower:.4f} (lower bound, zero output "
        f"tokens) to ${p.projected_cost_usd_upper:.4f} (upper bound, full max_tokens output) "
        "-- estimate_prompt_tokens under-counts, so the lower bound is a genuine floor, not "
        "an expected value"
    )
    lines.append(f"- detector_model: {p.detector_model}")
    lines.append(f"- comparator_model: {p.comparator_model}")
    lines.append("")

    max_usd_str = f" (ceiling ${report.max_usd:.2f})" if report.max_usd is not None else ""
    lines.append(f"- actual cost_usd this run: ${report.cost_usd:.4f}{max_usd_str}")
    lines.append("")

    return "\n".join(lines)


def write_report(
    report: ParityReport,
    *,
    out_dir: Path = DEFAULT_MEASUREMENTS_DIR,
    filename: str | None = None,
) -> Path:
    """Render *report* and write it under *out_dir* (created if missing).

    Default ``filename`` is ``shadow-parity-<YYYY-MM-DD>.md``, dated from
    ``report.generated``'s ISO-timestamp date prefix.

    NON-CLOBBERING (QA finding 5): a second run the same day is the common
    case for a run that stalls or aborts, and the single most likely
    "second run today" is an operator RETRY after a ``--max-usd`` abort —
    which would otherwise silently overwrite exactly the partial artifact
    the abort path exists to preserve, with no warning. If the target path
    already exists, a numeric suffix (``-2``, ``-3``, ...) is appended
    before the extension until an unused path is found. The base dated name
    is returned unchanged when there is no collision.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    if filename is None:
        filename = f"shadow-parity-{report.generated[:10]}.md"
    name_path = Path(filename)
    stem, suffix = name_path.stem, name_path.suffix
    path = out_dir / filename
    n = 2
    while path.exists():
        path = out_dir / f"{stem}-{n}{suffix}"
        n += 1
    path.write_text(render_report(report), encoding="utf-8")
    return path
