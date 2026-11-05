# SPDX-License-Identifier: Apache-2.0
"""Score the `used` heuristic (:func:`athenaeum.push_metrics.determine_references`)
against human labels (issue athenaeum#2302, part of the athenaeum#2295 epic).

The epic's problem statement: today's push-reference numbers conflate two
different causes of a pushed page going unused — **the push was irrelevant**
(a retrieval miss), or **the push was relevant and the pointer was ignored**
(a delivery-form problem). Those causes call for opposite fixes, and the
heuristic's only existing accuracy report
(``docs/measurements/used-column-heuristic-accuracy-2026-09-15.md``) is five
synthetic fixtures with no live session read, so its 0% error rates describe
the fixture design, not real accuracy. This module is the TOOLING that makes
a real measurement possible, in three pieces:

1. :func:`stratified_sample_pushed_pages` — picks pushed pages for a human to
   label, stratified by session-length bucket and (caller-supplied)
   population, from the existing push-records ledger
   (:func:`athenaeum.push_metrics.read_push_records`).
2. :class:`PushPageLabel` / :func:`load_labels` — the two-judgment label
   schema (``relevant``, ``changed_work``) and its JSONL file format.
3. :func:`score_against_heuristic` / :func:`score_labels` — compares the
   heuristic's verdict against the labels and reports precision, recall, and
   the relevant/irrelevant breakdown of unused pushes, all with confidence
   intervals.

This module never re-implements the `used` rule. :func:`compute_heuristic_verdicts`
drives the real :func:`athenaeum.push_metrics.determine_references` — the
same function the production ``session_end`` path calls — exactly like the
existing accuracy report's own stated principle ("The measurement drives the
real function; it never reimplements it").

**HARD SAFETY CONSTRAINT.** Real transcript content must never enter this
public repo, including as fixtures. Every fixture this module's tests use is
hand-authored synthetic data. Labelling real pushed pages is a separate issue
(athenaeum#2311) and a separate workflow; this module only builds the tool
that scores labels once they exist.

**Path rules (acceptance criteria, not convention).** Label files are always
read from a path the CALLER passes explicitly
(:func:`load_labels`/``--labels``, no default) — they live OUTSIDE this
repo, never inside the wiki corpus. Every default OUTPUT path
(:func:`default_sample_output_path`, :func:`default_score_output_path`) is
rooted under :func:`athenaeum.config.resolve_cache_dir` (``~/.cache/athenaeum``
by default) — never inside the repo working tree.

**Confidence interval: Wilson score interval, not Wald/normal-approximation.**
The Wald interval (``p_hat +/- z*sqrt(p_hat*(1-p_hat)/n)``) is the textbook
default but is a poor choice here: labelled samples for this measurement are
expected to be small (tens, not thousands, of pushed pages per stratum —
hand-labelling is the bottleneck, per athenaeum#2311), and Wald's coverage is
known to collapse exactly in that small-``n``/extreme-``p`` regime — it can
even emit bounds outside ``[0, 1]``. The Wilson score interval
(:func:`wilson_score_interval`) inverts the normal approximation to the
binomial test instead of centering a symmetric window on ``p_hat``, which
keeps it bounded in ``[0, 1]`` and keeps materially better coverage at small
``n`` — the standard textbook recommendation for a single proportion's CI
(e.g. Agresti & Coull 1998; Brown, Cai & DasGupta 2001) and the interval
``statsmodels.stats.proportion.proportion_confint`` defaults to. No new
third-party dependency is added for it: ``scipy`` is not in this repo's
dependency tree (checked against ``pyproject.toml`` before writing this),
so the closed-form Wilson formula is implemented directly against the
stdlib ``math`` module rather than reached for a library. The z-score table
in :data:`_Z_SCORES` covers the handful of confidence levels this tooling
needs (90/95/98/99%); an unlisted confidence level raises ``ValueError``
rather than silently approximating one, per this module's "loud failure"
convention.

Layering: L4 domain/pipeline module. Imports :mod:`athenaeum.push_metrics`
(L3, for :func:`~athenaeum.push_metrics.read_push_records` and
:func:`~athenaeum.push_metrics.determine_references`),
:mod:`athenaeum.config` (L2, cache-dir resolution), and
:mod:`athenaeum.store` (L1, ``now_iso``). Never imported back by
``push_metrics`` — this module is a CONSUMER of the ledger and the rule, not
a dependency of either.
"""

from __future__ import annotations

import json
import math
import random
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from athenaeum.config import resolve_cache_dir
from athenaeum.store import now_iso

#: Schema version stamped on the sample worksheet, the label file, and the
#: score report written by this module. Independent of
#: :data:`athenaeum.push_metrics.SCHEMA_VERSION` — this is a different
#: artifact family with its own evolution.
SCHEMA_VERSION = 1

#: Directory name under the cache dir every default output path in this
#: module roots under (see the module docstring's "Path rules").
_OUTPUT_SUBDIR = "push_used_scoring"

#: Two-sided z-scores for the confidence levels this tooling supports.
#: Deliberately a small fixed table, not a general inverse-normal-CDF
#: implementation — see the module docstring's CI section for why that
#: tradeoff is fine here (a handful of standard levels covers every real
#: use of this tool, and a wrong silent approximation is worse than a loud
#: "pick a supported level").
_Z_SCORES: dict[float, float] = {
    0.80: 1.2815515655446004,
    0.90: 1.6448536269514722,
    0.95: 1.959963984540054,
    0.98: 2.3263478740408408,
    0.99: 2.5758293035489004,
}

DEFAULT_CONFIDENCE_LEVEL = 0.95

#: Default session-length bucket boundaries, in PUSH-RECORD count for that
#: session (the only length-shaped signal the push-records ledger carries —
#: see :func:`session_length_bucket`). ``short``: <= this many push records;
#: ``medium``: <= this many; anything above is ``long``.
DEFAULT_SHORT_MAX_PUSHES = 3
DEFAULT_MEDIUM_MAX_PUSHES = 10

#: :func:`session_length_bucket` return values.
LENGTH_SHORT = "short"
LENGTH_MEDIUM = "medium"
LENGTH_LONG = "long"

#: Population label used when the caller supplies no
#: ``session_population`` mapping, or the mapping has no entry for a given
#: session. Named rather than an empty string so a report reader can tell
#: "no population signal was available" from "the population is the empty
#: string" (issue athenaeum#2295's own framing: no interactive/headless join
#: exists over this ledger today, so a caller that doesn't have one yet must
#: still be able to run this sampler).
POPULATION_UNKNOWN = "unknown"


# ---------------------------------------------------------------------------
# Wilson score interval
# ---------------------------------------------------------------------------


def wilson_score_interval(
    successes: int, n: int, *, confidence: float = DEFAULT_CONFIDENCE_LEVEL
) -> tuple[float, float] | None:
    """Wilson score confidence interval for a binomial proportion.

    Returns ``(lower, upper)``, both clamped to ``[0, 1]``, or ``None`` when
    ``n == 0`` (no sample means no interval, not a degenerate ``(0, 1)``
    placeholder). See the module docstring for why Wilson rather than the
    Wald normal approximation.

    Raises ``ValueError`` for an unsupported *confidence* (see
    :data:`_Z_SCORES`) or an impossible ``(successes, n)`` pair.
    """
    if n < 0 or successes < 0 or successes > n:
        raise ValueError(f"invalid (successes={successes}, n={n}): need 0 <= successes <= n")
    if n == 0:
        return None
    try:
        z = _Z_SCORES[confidence]
    except KeyError:
        supported = ", ".join(str(c) for c in sorted(_Z_SCORES))
        raise ValueError(
            f"unsupported confidence level {confidence!r} — supported: {supported}"
        ) from None
    p_hat = successes / n
    z2 = z * z
    denom = 1 + z2 / n
    center = p_hat + z2 / (2 * n)
    margin = z * math.sqrt((p_hat * (1 - p_hat) + z2 / (4 * n)) / n)
    lower = (center - margin) / denom
    upper = (center + margin) / denom
    return (max(0.0, lower), min(1.0, upper))


@dataclass(frozen=True)
class RateEstimate:
    """A rate (successes / n) plus its Wilson confidence interval.

    Rates and counts ONLY — never an id, page title, or query. This is the
    leaf type every count-of-something figure in :class:`ScoreReport` is
    built from, so keeping it identifier-free here is what keeps the whole
    report identifier-free (see ``tests/test_push_used_scoring.py::
    test_score_report_contains_no_identifiers``).
    """

    successes: int
    n: int
    confidence: float
    rate: float | None
    ci_low: float | None
    ci_high: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "successes": self.successes,
            "n": self.n,
            "confidence": self.confidence,
            "rate": self.rate,
            "ci_low": self.ci_low,
            "ci_high": self.ci_high,
        }


def _rate_estimate(
    successes: int, n: int, *, confidence: float = DEFAULT_CONFIDENCE_LEVEL
) -> RateEstimate:
    if n == 0:
        return RateEstimate(
            successes=0, n=0, confidence=confidence, rate=None, ci_low=None, ci_high=None
        )
    ci = wilson_score_interval(successes, n, confidence=confidence)
    assert ci is not None  # n > 0 here, so wilson_score_interval never returns None
    return RateEstimate(
        successes=successes,
        n=n,
        confidence=confidence,
        rate=successes / n,
        ci_low=ci[0],
        ci_high=ci[1],
    )


# ---------------------------------------------------------------------------
# Stratified sampler
# ---------------------------------------------------------------------------


def session_length_bucket(
    push_record_count: int,
    *,
    short_max: int = DEFAULT_SHORT_MAX_PUSHES,
    medium_max: int = DEFAULT_MEDIUM_MAX_PUSHES,
) -> str:
    """Bucket a session's PUSH-RECORD COUNT into short/medium/long.

    This is a proxy for "session length", not a measured session duration or
    turn count — the push-records ledger carries no such field (the same gap
    athenaeum#2295's problem statement notes for the interactive/headless
    population split). Using the count of push records a session accumulated
    is the one length-shaped signal the ledger actually has, so this sampler
    uses it honestly, named as a bucket boundary over push-record count
    rather than implying a stronger measurement.
    """
    if push_record_count <= short_max:
        return LENGTH_SHORT
    if push_record_count <= medium_max:
        return LENGTH_MEDIUM
    return LENGTH_LONG


@dataclass(frozen=True)
class SampledPage:
    """One pushed page selected for labelling, plus the stratum it was drawn
    from. Carries only ids and the stratum descriptors — no page text, no
    query text (the push-records ledger never has either to carry).
    """

    session_id: str
    push_id: str
    length_bucket: str
    population: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "push_id": self.push_id,
            "length_bucket": self.length_bucket,
            "population": self.population,
            # Present so the labeller's file can be filled in directly from
            # this worksheet without a separate schema lookup; see
            # load_labels()/write_sample_worksheet().
            "relevant": None,
            "changed_work": None,
        }


def stratified_sample_pushed_pages(
    cache_dir: Path | None = None,
    *,
    n_per_stratum: int,
    wiki_root: Path | None = None,
    session_population: Mapping[str, str] | None = None,
    short_max: int = DEFAULT_SHORT_MAX_PUSHES,
    medium_max: int = DEFAULT_MEDIUM_MAX_PUSHES,
    seed: int | None = None,
    exclude_sessions: Iterable[str] | None = None,
) -> list[SampledPage]:
    """Sample up to *n_per_stratum* pushed pages from EACH (length, population)
    stratum found in the push-records ledger.

    One pushed page is one distinct ``(session_id, push_id)`` pair — a page
    pushed twice to the same session counts once (it is one labelling
    decision, not two). Stratification is by the pushing session's
    :func:`session_length_bucket` crossed with *session_population* (a
    caller-supplied ``session_id -> population label`` mapping; sessions
    absent from it, or when no mapping is given at all, fall into
    :data:`POPULATION_UNKNOWN` — issue athenaeum#2295 found no existing
    interactive/headless field on this ledger, so this sampler does not
    invent one).

    Deterministic when *seed* is given (test seam); otherwise a fresh random
    draw per stratum each call. ``exclude_sessions`` matches
    :func:`athenaeum.push_metrics.build_coverage_worksheet`'s
    ``--exclude-session`` semantics exactly (PR review finding on
    athenaeum#2302: an earlier version matched *exclude_sessions* by exact
    string only, so a session-id PREFIX or typo matched nothing and
    exited/returned clean with NO filtering applied — a contaminated sample
    that looked fine, the precise athenaeum#987 failure shape
    :func:`athenaeum.push_metrics._resolve_exclude_sessions` exists to
    forbid). Each requested value must resolve to exactly one session id
    actually present in the ledger — the full id, or an unambiguous
    prefix — else this raises ``ValueError`` naming the ambiguous/unmatched
    value; it never silently excludes nothing. Resolved exclusions are
    removed from the pool before stratification, never merely from the
    final sample.

    Returns pages sorted by ``(length_bucket, population, session_id,
    push_id)`` for a deterministic, diffable worksheet.
    """
    from athenaeum import push_metrics

    records = push_metrics.read_push_records(cache_dir, wiki_root=wiki_root)
    known_session_ids = {
        sid for r in records if isinstance(sid := r.get("session_id"), str) and sid
    }
    # Calls push_metrics' own private helper directly (same cross-module
    # reuse precedent as athenaeum.context calling push_metrics._query_hash)
    # rather than re-deriving the athenaeum#987 prefix-resolution rule here.
    exclude_set = push_metrics._resolve_exclude_sessions(exclude_sessions or (), known_session_ids)
    population_map = session_population or {}

    pushes_per_session: dict[str, int] = {}
    pages_per_session: dict[str, set[str]] = {}
    for rec in records:
        sid = rec.get("session_id")
        if not isinstance(sid, str) or not sid or sid in exclude_set:
            continue
        pushes_per_session[sid] = pushes_per_session.get(sid, 0) + 1
        pages = pages_per_session.setdefault(sid, set())
        for item in rec.get("items", []):
            pid = item.get("id")
            if isinstance(pid, str) and pid:
                pages.add(pid)

    strata: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for sid, push_count in pushes_per_session.items():
        bucket = session_length_bucket(push_count, short_max=short_max, medium_max=medium_max)
        population = population_map.get(sid, POPULATION_UNKNOWN)
        stratum_key = (bucket, population)
        stratum_pairs = strata.setdefault(stratum_key, [])
        for pid in sorted(pages_per_session.get(sid, ())):
            stratum_pairs.append((sid, pid))

    rng = random.Random(seed)
    sampled: list[SampledPage] = []
    for (bucket, population), stratum_pairs in strata.items():
        chosen = (
            stratum_pairs
            if len(stratum_pairs) <= n_per_stratum
            else rng.sample(stratum_pairs, n_per_stratum)
        )
        for sid, pid in chosen:
            sampled.append(
                SampledPage(
                    session_id=sid, push_id=pid, length_bucket=bucket, population=population
                )
            )
    sampled.sort(key=lambda p: (p.length_bucket, p.population, p.session_id, p.push_id))
    return sampled


def default_sample_output_path(cache_dir: Path | None = None) -> Path:
    """Default sample-worksheet path — always under
    :func:`athenaeum.config.resolve_cache_dir`, never inside the repo
    working tree (athenaeum#2302 acceptance criterion).
    """
    return resolve_cache_dir(cache_dir) / _OUTPUT_SUBDIR / "sample-for-labelling.jsonl"


def write_sample_worksheet(pages: Sequence[SampledPage], *, output_path: Path) -> Path:
    """Write *pages* as a labelling-ready JSONL worksheet at *output_path*.

    *output_path* is always an explicit argument — this function sets no
    default of its own (see :func:`default_sample_output_path` for the one
    place a default is computed, and the module docstring's "Path rules").
    Each line is one :meth:`SampledPage.to_dict` — ``relevant``/
    ``changed_work`` start ``null``, ready for a human labeller to fill in
    and hand to :func:`load_labels`.
    """
    from athenaeum.atomic_io import atomic_write_text

    lines = [json.dumps(page.to_dict(), sort_keys=True) for page in pages]
    atomic_write_text(output_path, "\n".join(lines) + ("\n" if lines else ""))
    return output_path


# ---------------------------------------------------------------------------
# Label schema
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PushPageLabel:
    """One human-labelled judgment of a pushed page (issue athenaeum#2302).

    Two judgments, matching the issue's scope exactly:

    - ``relevant`` — was the page relevant to the session it was pushed to?
    - ``changed_work`` — did it change the work (the session's output):
      genuine use, as opposed to a relevant page the session never engaged
      with.

    ``changed_work`` is the ground-truth analog the `used` heuristic is
    trying to detect (citation or content reproduction is itself evidence
    the page changed the work), so :func:`score_labels` treats
    ``changed_work`` as the positive class. ``relevant`` carries no
    precision/recall role by itself — it exists to split the pages the
    heuristic calls unused into the two causes the athenaeum#2295 epic
    exists to tell apart (see :class:`UnusedBreakdown`).

    Deliberately carries no page text and no query text — only ids and the
    two booleans — so a label FILE (which this module never reads a default
    path for) cannot itself become a vector for real transcript content
    leaking anywhere this tooling touches.
    """

    session_id: str
    push_id: str
    relevant: bool
    changed_work: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "push_id": self.push_id,
            "relevant": self.relevant,
            "changed_work": self.changed_work,
        }


def _label_from_dict(raw: dict[str, Any], *, line_no: int, path: Path) -> PushPageLabel:
    def _require_str(key: str) -> str:
        value = raw.get(key)
        if not isinstance(value, str) or not value:
            raise ValueError(
                f"{path}:{line_no}: missing or non-string '{key}' — "
                f"each label line needs a non-empty session_id and push_id"
            )
        return value

    def _require_bool(key: str) -> bool:
        value = raw.get(key)
        if isinstance(value, bool):
            return value
        if value is None:
            raise ValueError(
                f"{path}:{line_no}: '{key}' is unlabelled (null) — "
                f"every line must be fully labelled before scoring; "
                f"finish labelling or drop this line before re-running the scorer"
            )
        raise ValueError(f"{path}:{line_no}: '{key}' must be a boolean, got {value!r}")

    return PushPageLabel(
        session_id=_require_str("session_id"),
        push_id=_require_str("push_id"),
        relevant=_require_bool("relevant"),
        changed_work=_require_bool("changed_work"),
    )


def load_labels(path: Path) -> list[PushPageLabel]:
    """Load fully-labelled :class:`PushPageLabel` rows from a JSONL file.

    *path* is always an explicit caller-supplied argument — this function has
    no default of its own and reads nothing else. Label files live OUTSIDE
    this repo (athenaeum#2302 acceptance criterion); this reader does not
    care where *path* points, but nothing in this module ever defaults one
    inside the repo working tree.

    Raises ``ValueError`` (naming the file and line) on a malformed line or
    an unlabelled (``null``) judgment — a partially-labelled file is a loud
    failure here, never a silent skip, so a scoring run can't under-count
    against a worksheet someone forgot to finish.
    """
    labels: list[PushPageLabel] = []
    text = path.read_text(encoding="utf-8")
    for line_no, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{line_no}: invalid JSON ({exc})") from exc
        if not isinstance(raw, dict):
            raise ValueError(f"{path}:{line_no}: expected a JSON object, got {type(raw).__name__}")
        labels.append(_label_from_dict(raw, line_no=line_no, path=path))
    return labels


# ---------------------------------------------------------------------------
# Scorer
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConfusionCounts:
    """The known-by-construction confusion matrix cells.

    Ground truth positive = :attr:`PushPageLabel.changed_work`. Heuristic
    positive = the pushed id appears in
    :attr:`athenaeum.push_metrics.ReferenceResult.referenced_ids` for that
    session (i.e. the `used` heuristic's own verdict — never re-derived here,
    see :func:`compute_heuristic_verdicts`).
    """

    tp: int
    fp: int
    fn: int
    tn: int

    @property
    def total(self) -> int:
        return self.tp + self.fp + self.fn + self.tn

    def to_dict(self) -> dict[str, Any]:
        return {"tp": self.tp, "fp": self.fp, "fn": self.fn, "tn": self.tn, "total": self.total}


@dataclass(frozen=True)
class UnusedBreakdown:
    """Among pushed pages the LABELS say were never used
    (``changed_work=False``): the split the athenaeum#2295 epic exists to
    make — a page that was **relevant but not opened** (an ignored pointer;
    the fix is delivery form) versus a page that was **irrelevant** (a
    retrieval miss; the fix is retrieval). First-class output field, not a
    derived afterthought: every reader of a :class:`ScoreReport` sees this
    split directly, without recomputing it from the raw labels.

    The two counts always partition ``unused_total`` exactly — every
    ``changed_work=False`` label has a definite ``relevant`` value, so there
    is no third bucket and no overlap.
    """

    unused_total: int
    relevant_but_not_opened: RateEstimate
    irrelevant: RateEstimate

    def to_dict(self) -> dict[str, Any]:
        return {
            "unused_total": self.unused_total,
            "relevant_but_not_opened": self.relevant_but_not_opened.to_dict(),
            "irrelevant": self.irrelevant.to_dict(),
        }


@dataclass(frozen=True)
class ScoreReport:
    """The scorer's full output: rates and counts only — no session id, push
    id, page text, or query text anywhere in :meth:`to_dict` (see
    ``tests/test_push_used_scoring.py::test_score_report_contains_no_identifiers``).

    ``undetermined_labels``/``undetermined_sessions`` (PR review finding on
    athenaeum#2302): count of labelled pages / distinct sessions the `used`
    heuristic could not determine a verdict for at all (see
    :func:`score_labels`'s docstring) and which were therefore EXCLUDED from
    ``confusion``/``precision``/``recall``/``unused_breakdown`` rather than
    silently counted as a heuristic negative. ``confusion.total +
    undetermined_labels == sample_size`` always holds.
    """

    v: int
    generated: str
    confidence: float
    sample_size: int
    confusion: ConfusionCounts
    precision: RateEstimate
    recall: RateEstimate
    unused_breakdown: UnusedBreakdown
    undetermined_labels: int
    undetermined_sessions: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "v": self.v,
            "generated": self.generated,
            "confidence": self.confidence,
            "sample_size": self.sample_size,
            "confusion": self.confusion.to_dict(),
            "precision": self.precision.to_dict(),
            "recall": self.recall.to_dict(),
            "unused_breakdown": self.unused_breakdown.to_dict(),
            "undetermined_labels": self.undetermined_labels,
            "undetermined_sessions": self.undetermined_sessions,
        }


def score_labels(
    labels: Sequence[PushPageLabel],
    heuristic_used: Mapping[tuple[str, str], bool | None],
    *,
    confidence: float = DEFAULT_CONFIDENCE_LEVEL,
) -> ScoreReport:
    """Pure scorer: compare already-computed *heuristic_used* verdicts
    against *labels*.

    *heuristic_used* maps ``(session_id, push_id) -> bool | None`` — the
    `used` heuristic's own verdict for that pushed page, as already decided
    by :func:`athenaeum.push_metrics.determine_references` (or, for a known-
    confusion-matrix test, hand-constructed directly):

    - ``True``/``False`` — the heuristic DETERMINED the page was/was not
      referenced. Classified into the confusion matrix as normal.
    - ``None`` — the heuristic could not determine an answer at all (the
      session's transcript rolled off disk, was never found, or carried no
      push records — see :func:`compute_heuristic_verdicts`). This is
      excluded from the confusion matrix entirely rather than folded into
      ``False``: a prior version of this scorer collapsed "undetermined"
      into "the heuristic said no" (PR review finding on athenaeum#2302),
      which silently biased the headline numbers — a ``changed_work=True``
      label would count as a false negative (deflating recall) purely
      because its transcript was unreadable, not because the heuristic
      actually looked and found nothing. That is exactly the kind of
      fixture-shaped, not-reality-shaped number this tool exists to retire
      (see this module's docstring). Every excluded label is still counted,
      in :attr:`ScoreReport.undetermined_labels` /
      :attr:`ScoreReport.undetermined_sessions` — an excluded sample that is
      not itself reported would just move the silent bias one level up.
    - absent entirely — treated as ``False`` (not used), not as an error: a
      page the heuristic never saw IS a heuristic negative. This is the
      seam the known-confusion-matrix test uses to hand-build inputs without
      an undetermined case in the loop.

    This function never calls any production ledger or transcript code — see
    :func:`score_against_heuristic` for the wrapper that drives the real
    heuristic. Kept separate deliberately so a test can assert an EXACT
    confusion matrix and EXACT precision/recall against hand-built inputs,
    with no ledger or transcript fixture in the loop at all.
    """
    tp = fp = fn = tn = 0
    relevant_not_opened = 0
    irrelevant_unused = 0
    unused_total = 0
    undetermined_labels = 0
    undetermined_sessions: set[str] = set()
    for label in labels:
        used = heuristic_used.get((label.session_id, label.push_id), False)
        if used is None:
            undetermined_labels += 1
            undetermined_sessions.add(label.session_id)
            continue
        if label.changed_work:
            if used:
                tp += 1
            else:
                fn += 1
        else:
            unused_total += 1
            if used:
                fp += 1
            else:
                tn += 1
            if label.relevant:
                relevant_not_opened += 1
            else:
                irrelevant_unused += 1

    confusion = ConfusionCounts(tp=tp, fp=fp, fn=fn, tn=tn)
    precision = _rate_estimate(tp, tp + fp, confidence=confidence)
    recall = _rate_estimate(tp, tp + fn, confidence=confidence)
    breakdown = UnusedBreakdown(
        unused_total=unused_total,
        relevant_but_not_opened=_rate_estimate(
            relevant_not_opened, unused_total, confidence=confidence
        ),
        irrelevant=_rate_estimate(irrelevant_unused, unused_total, confidence=confidence),
    )
    return ScoreReport(
        v=SCHEMA_VERSION,
        generated=now_iso(),
        confidence=confidence,
        sample_size=len(labels),
        confusion=confusion,
        precision=precision,
        recall=recall,
        unused_breakdown=breakdown,
        undetermined_labels=undetermined_labels,
        undetermined_sessions=len(undetermined_sessions),
    )


def compute_heuristic_verdicts(
    labels: Sequence[PushPageLabel],
    *,
    cache_dir: Path | None = None,
    projects_root: Path | None = None,
    wiki_root: Path | None = None,
) -> dict[tuple[str, str], bool | None]:
    """Compute the `used` heuristic's verdict for every labelled page by
    calling the REAL :func:`athenaeum.push_metrics.determine_references` —
    exactly once per distinct session among *labels*, never reimplementing
    the rule.

    Returns ``(session_id, push_id) -> bool | None``: ``True``/``False`` is
    the heuristic's DETERMINED verdict; ``None`` means
    :func:`~athenaeum.push_metrics.determine_references` itself returned
    ``None`` for that session (no push records, no pushed ids, or the
    transcript could not be located/read — an honest "cannot determine",
    never a fabricated 0 or 1, per that function's own docstring). Collapsing
    ``None`` into "not referenced" here would silently turn "undetermined"
    into a heuristic negative — see :func:`score_labels`'s docstring for why
    that is a real bias this scorer must not reproduce, not a simplification.
    """
    from athenaeum import push_metrics

    sessions = sorted({label.session_id for label in labels})
    referenced_by_session: dict[str, set[str] | None] = {}
    for session_id in sessions:
        result = push_metrics.determine_references(
            session_id, cache_dir=cache_dir, projects_root=projects_root, wiki_root=wiki_root
        )
        referenced_by_session[session_id] = set(result.referenced_ids) if result else None

    verdicts: dict[tuple[str, str], bool | None] = {}
    for label in labels:
        referenced = referenced_by_session.get(label.session_id)
        key = (label.session_id, label.push_id)
        verdicts[key] = None if referenced is None else label.push_id in referenced
    return verdicts


def score_against_heuristic(
    labels: Sequence[PushPageLabel],
    *,
    cache_dir: Path | None = None,
    projects_root: Path | None = None,
    wiki_root: Path | None = None,
    confidence: float = DEFAULT_CONFIDENCE_LEVEL,
) -> ScoreReport:
    """Score *labels* against the REAL `used` heuristic end to end:
    :func:`compute_heuristic_verdicts` then :func:`score_labels`.

    This is the entry point production callers (and the ``athenaeum
    push-metrics score-used`` CLI) use; tests that need an exact known
    confusion matrix should call :func:`score_labels` directly with a
    hand-built *heuristic_used* mapping instead, to keep the math under test
    independent of ledger/transcript fixtures.
    """
    verdicts = compute_heuristic_verdicts(
        labels, cache_dir=cache_dir, projects_root=projects_root, wiki_root=wiki_root
    )
    return score_labels(labels, verdicts, confidence=confidence)


def default_score_output_path(cache_dir: Path | None = None) -> Path:
    """Default score-report path — always under
    :func:`athenaeum.config.resolve_cache_dir`, never inside the repo
    working tree (athenaeum#2302 acceptance criterion, same rule as
    :func:`default_sample_output_path`).
    """
    return resolve_cache_dir(cache_dir) / _OUTPUT_SUBDIR / "score-report.json"


def write_score_report(report: ScoreReport, *, output_path: Path) -> Path:
    """Write *report* as a durable JSON file at *output_path* (never
    console-only) — *output_path* is always an explicit argument; see
    :func:`default_score_output_path` for the one default this module
    computes.
    """
    from athenaeum.atomic_io import atomic_write_text

    atomic_write_text(output_path, json.dumps(report.to_dict(), indent=2, sort_keys=False) + "\n")
    return output_path
