# SPDX-License-Identifier: Apache-2.0
"""Recall-metrics scorer: do we push exactly what a task needs (athenaeum#2611,
epic athenaeum#2295).

The raw push-use rate (share of pushed pages marked ``used``) cannot say
whether push works: a baseline that listed every page would score near zero on
it, so a low rate does not mean push is failing. This module computes four
measures that need a **gold set** -- tasks whose relevant pages were labelled
independently of what was pushed -- and are reported per task, then
aggregated with confidence intervals. Definitions, the gold-set format, and
what each measure can and cannot show live in ``docs/design/recall-metrics.md``.

========================  ===================================================
push coverage             relevant pages that were pushed / relevant pages
recall-through            relevant pages the agent recalled or opened /
                          relevant pages (plus the pushed-only variant)
use precision             recalled-or-opened pages that were used / recalled-
                          or-opened pages
session use rate          tasks (sessions) that used >=1 delivered item / tasks
========================  ===================================================

**Inputs.** A :class:`GoldSet` (``load_gold_set`` -- YAML, validated) and one
:class:`TaskObservation` per task: the ids pushed, the ids recalled/opened, and
the ids used. :func:`observe_task` builds an observation from the push ledger
(:func:`athenaeum.push_metrics.read_push_records`) and the pushing session's
transcript, reusing :mod:`athenaeum.follow_through`'s open-event reader rather
than parsing transcripts a second time. :func:`score` is pure -- every test
runs it on synthetic observations.

**"Used"** is the authored-text citation rule: a delivered page is used when
its id is a whole token in USER or ASSISTANT text (never a tool result, which
would be the push's own echo). It misses silent application and content-only
use; ``docs/design/recall-metrics.md`` records the known error rate and where
it is (not) measured.

**Two limits carried over from the athenaeum#2306 report.**

* *Opened without push.* A relevant page the agent found by itself (opened
  without ever being pushed) is a real recall-through success but says nothing
  about push. It is counted in ``recall_through`` and reported separately as
  ``opened_without_push`` so it can never be mistaken for push working;
  ``recall_through_of_pushed`` counts only relevant pages that were pushed
  first and opened strictly after.
* *Opened counts a listing.* An id appearing in a ``recall`` tool result is
  scored as recalled even when it sat in a multi-hit listing the agent never
  read closely, so ``recall_through`` is an UPPER bound on deliberate reads.
  Because the same engaged set is the denominator of use precision, a
  listing-inflated engaged set shows up as lower use precision, not as hidden
  success. This module does not try to tell a listing from a read.

**Confidence intervals.** Every aggregate carries a Wilson interval over the
pooled counts (:func:`athenaeum.push_used_scoring.wilson_score_interval`) and
a task-level cluster bootstrap interval (pages within one task are not
independent, so the Wilson interval alone is optimistic). The bootstrap is
seeded, so a score is reproducible.

**No model calls, no real transcript content.** Output carries counts, rates
and task ids from the gold set only -- never a session id, page id or text
from a transcript.

Layering: L4 domain/pipeline. Imports :mod:`athenaeum.follow_through` (L4,
sibling), :mod:`athenaeum.push_metrics` (L3), :mod:`athenaeum.push_used_scoring`
(L4, sibling) and :mod:`athenaeum.transcript_verify` (L1). Nothing imports it.
"""

from __future__ import annotations

import random
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from athenaeum import follow_through, push_metrics, transcript_verify
from athenaeum.push_used_scoring import DEFAULT_CONFIDENCE_LEVEL, wilson_score_interval

GOLD_SET_VERSION = 1
REACH_LEXICAL = "lexical"
REACH_LINKED = "linked"
REACH_VALUES = (REACH_LEXICAL, REACH_LINKED)
RELEVANCE_REQUIRED = "required"
RELEVANCE_HELPFUL = "helpful"
RELEVANCE_VALUES = (RELEVANCE_REQUIRED, RELEVANCE_HELPFUL)

METRIC_NAMES = (
    "push_coverage",
    "recall_through",
    "recall_through_of_pushed",
    "use_precision",
    "session_use_rate",
)

DEFAULT_BOOTSTRAP_RESAMPLES = 2000
DEFAULT_BOOTSTRAP_SEED = 2611


class GoldSetError(ValueError):
    """The gold-set file does not match the documented schema."""


# --------------------------------------------------------------------- gold set


@dataclass(frozen=True)
class RelevantPage:
    """One labelled page: ``uid`` is the id form the push ledger records."""

    uid: str
    source: str
    relevance: str = RELEVANCE_REQUIRED
    reach: str = REACH_LEXICAL


@dataclass(frozen=True)
class GoldTask:
    task_id: str
    prompt: str
    relevant: tuple[RelevantPage, ...]
    session_id: str | None = None

    def relevant_ids(self, *, include_helpful: bool = True) -> frozenset[str]:
        return frozenset(
            p.uid
            for p in self.relevant
            if include_helpful or p.relevance == RELEVANCE_REQUIRED
        )


@dataclass(frozen=True)
class GoldSet:
    tasks: tuple[GoldTask, ...]
    corpus: str = ""


def _req_str(raw: Mapping[str, Any], key: str, where: str) -> str:
    val = raw.get(key)
    if not isinstance(val, str) or not val.strip():
        raise GoldSetError(f"{where}: '{key}' must be a non-empty string")
    return val


def parse_gold_set(data: object) -> GoldSet:
    """Validate an already-loaded gold-set mapping (schema in the design doc)."""
    if not isinstance(data, Mapping):
        raise GoldSetError("gold set must be a mapping")
    if data.get("version") != GOLD_SET_VERSION:
        raise GoldSetError(f"'version' must be {GOLD_SET_VERSION}")
    raw_tasks = data.get("tasks")
    if not isinstance(raw_tasks, list) or not raw_tasks:
        raise GoldSetError("'tasks' must be a non-empty list")
    tasks: list[GoldTask] = []
    seen: set[str] = set()
    for i, raw in enumerate(raw_tasks):
        where = f"tasks[{i}]"
        if not isinstance(raw, Mapping):
            raise GoldSetError(f"{where}: must be a mapping")
        task_id = _req_str(raw, "id", where)
        if task_id in seen:
            raise GoldSetError(f"{where}: duplicate task id {task_id!r}")
        seen.add(task_id)
        prompt = _req_str(raw, "prompt", where)
        raw_rel = raw.get("relevant")
        if not isinstance(raw_rel, list) or not raw_rel:
            raise GoldSetError(f"{where}: 'relevant' must be a non-empty list")
        pages: list[RelevantPage] = []
        for j, rp in enumerate(raw_rel):
            rwhere = f"{where}.relevant[{j}]"
            if not isinstance(rp, Mapping):
                raise GoldSetError(f"{rwhere}: must be a mapping")
            relevance = rp.get("relevance", RELEVANCE_REQUIRED)
            reach = rp.get("reach", REACH_LEXICAL)
            if relevance not in RELEVANCE_VALUES:
                raise GoldSetError(f"{rwhere}: 'relevance' must be one of {RELEVANCE_VALUES}")
            if reach not in REACH_VALUES:
                raise GoldSetError(f"{rwhere}: 'reach' must be one of {REACH_VALUES}")
            pages.append(
                RelevantPage(
                    uid=_req_str(rp, "uid", rwhere),
                    source=_req_str(rp, "source", rwhere),
                    relevance=relevance,
                    reach=reach,
                )
            )
        if len({p.uid for p in pages}) != len(pages):
            raise GoldSetError(f"{where}: duplicate uid in 'relevant'")
        session_id = raw.get("session_id")
        if session_id is not None and not isinstance(session_id, str):
            raise GoldSetError(f"{where}: 'session_id' must be a string when present")
        tasks.append(GoldTask(task_id, prompt, tuple(pages), session_id))
    corpus = data.get("corpus", "")
    return GoldSet(tasks=tuple(tasks), corpus=corpus if isinstance(corpus, str) else "")


def load_gold_set(path: Path) -> GoldSet:
    """Read and validate a gold-set YAML file. The path is always caller-given."""
    return parse_gold_set(yaml.safe_load(path.read_text(encoding="utf-8")))


# ------------------------------------------------------------------ observation


@dataclass(frozen=True)
class TaskObservation:
    """What happened in the task's session, as sets of page ids.

    ``recalled`` is every id the agent recalled or opened (after its own
    push for a pushed id; at any time for an unpushed one). ``used`` is the
    ids cited in authored text. ``opened_without_push`` is NOT a separate
    input: it is derived in :func:`score_task` as ``recalled - pushed``.
    """

    task_id: str
    pushed: frozenset[str] = frozenset()
    recalled: frozenset[str] = frozenset()
    used: frozenset[str] = frozenset()


def observe_task(
    task: GoldTask,
    *,
    push_records: Iterable[dict[str, Any]],
    transcript_records: Sequence[object],
) -> TaskObservation:
    """Build one task's observation from its session's push + transcript records.

    Only ledger rows for ``task.session_id`` are read. Recall events are
    matched against the session's pushed ids plus the task's gold ids, so a
    relevant page recalled without ever being pushed is still seen.
    """
    session_id = task.session_id
    if not session_id:
        raise GoldSetError(f"task {task.task_id!r} has no session_id to observe")
    pushed_at = follow_through._pushed_ids_by_session(push_records).get(session_id, {})
    gold_ids = task.relevant_ids()
    events = follow_through._collect_open_events(
        transcript_records, known_ids=set(pushed_at) | gold_ids
    )
    recalled: set[str] = set()
    for ts, pid in events:
        if pid in pushed_at:
            if ts is not None and _strictly_after(ts, pushed_at[pid]):
                recalled.add(pid)
        else:
            recalled.add(pid)
    author_blob, _assistant, _echo = push_metrics.split_transcript_text_by_provenance(
        transcript_records
    )
    delivered = set(pushed_at) | recalled
    used = {pid for pid in delivered if push_metrics._appears_as_whole_token(pid, author_blob)}
    return TaskObservation(
        task_id=task.task_id,
        pushed=frozenset(pushed_at),
        recalled=frozenset(recalled),
        used=frozenset(used),
    )


def _strictly_after(open_ts: datetime, push_ts: datetime) -> bool:
    return follow_through._opened_strictly_after(open_ts, push_ts)


def observe_gold_set(
    gold: GoldSet,
    *,
    cache_dir: Path | None = None,
    projects_root: Path | None = None,
) -> tuple[list[TaskObservation], list[str]]:
    """Observe every task that names a ``session_id``.

    Returns ``(observations, unobserved_task_ids)``; a task with no session id,
    or whose transcript cannot be found, is listed in the second element and
    never scored (no observation is not "nothing pushed").
    """
    root = projects_root or transcript_verify.default_projects_root()
    push_records = push_metrics.read_push_records(cache_dir)
    observations: list[TaskObservation] = []
    skipped: list[str] = []
    for task in gold.tasks:
        if not task.session_id:
            skipped.append(task.task_id)
            continue
        located = push_metrics._find_session_transcript(task.session_id, root)
        records = (
            transcript_verify._iter_session_records(located[0], task.session_id)
            if located
            else []
        )
        if not records:
            skipped.append(task.task_id)
            continue
        observations.append(
            observe_task(task, push_records=push_records, transcript_records=records)
        )
    return observations, skipped


# ---------------------------------------------------------------------- scoring


@dataclass(frozen=True)
class TaskScore:
    """One task's counts. A rate with a zero denominator is ``None``, never 0."""

    task_id: str
    n_relevant: int
    relevant_pushed: int
    relevant_recalled: int
    relevant_pushed_then_opened: int
    engaged: int
    engaged_used: int
    session_used: bool
    pushed_total: int
    opened_without_push: int
    relevant_linked_only: int
    relevant_linked_pushed: int

    def rate(self, name: str) -> float | None:
        num, den = self._ratio(name)
        return None if den == 0 else num / den

    def _ratio(self, name: str) -> tuple[int, int]:
        if name == "push_coverage":
            return self.relevant_pushed, self.n_relevant
        if name == "recall_through":
            return self.relevant_recalled, self.n_relevant
        if name == "recall_through_of_pushed":
            return self.relevant_pushed_then_opened, self.relevant_pushed
        if name == "use_precision":
            return self.engaged_used, self.engaged
        if name == "session_use_rate":
            return int(self.session_used), 1
        raise KeyError(name)


def score_task(task: GoldTask, obs: TaskObservation) -> TaskScore:
    if obs.task_id != task.task_id:
        raise ValueError(f"observation {obs.task_id!r} is not for task {task.task_id!r}")
    rel = task.relevant_ids()
    linked = {p.uid for p in task.relevant if p.reach == REACH_LINKED}
    engaged = obs.recalled
    delivered = obs.pushed | obs.recalled
    return TaskScore(
        task_id=task.task_id,
        n_relevant=len(rel),
        relevant_pushed=len(rel & obs.pushed),
        relevant_recalled=len(rel & obs.recalled),
        relevant_pushed_then_opened=len(rel & obs.pushed & obs.recalled),
        engaged=len(engaged),
        engaged_used=len(engaged & obs.used),
        session_used=bool(delivered & obs.used),
        pushed_total=len(obs.pushed),
        opened_without_push=len(obs.recalled - obs.pushed),
        relevant_linked_only=len(linked),
        relevant_linked_pushed=len(linked & obs.pushed),
    )


@dataclass(frozen=True)
class Estimate:
    """Pooled estimate with a Wilson interval and a task-cluster bootstrap one."""

    numerator: int
    denominator: int
    n_tasks: int
    rate: float | None
    wilson: tuple[float, float] | None
    bootstrap: tuple[float, float] | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "numerator": self.numerator,
            "denominator": self.denominator,
            "n_tasks": self.n_tasks,
            "rate": self.rate,
            "wilson": list(self.wilson) if self.wilson else None,
            "bootstrap": list(self.bootstrap) if self.bootstrap else None,
        }


def _percentile(sorted_vals: list[float], q: float) -> float:
    idx = q * (len(sorted_vals) - 1)
    lo = int(idx)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (idx - lo)


def _estimate(
    pairs: list[tuple[int, int]],
    *,
    confidence: float,
    resamples: int,
    seed: int,
) -> Estimate:
    """*pairs* = one ``(numerator, denominator)`` per task that has a defined rate."""
    pairs = [p for p in pairs if p[1] > 0]
    num = sum(p[0] for p in pairs)
    den = sum(p[1] for p in pairs)
    if den == 0:
        return Estimate(0, 0, len(pairs), None, None, None)
    boot: tuple[float, float] | None = None
    if len(pairs) >= 2 and resamples > 0:
        rng = random.Random(seed)
        draws: list[float] = []
        for _ in range(resamples):
            sample = [pairs[rng.randrange(len(pairs))] for _ in pairs]
            d = sum(s[1] for s in sample)
            if d:
                draws.append(sum(s[0] for s in sample) / d)
        if draws:
            draws.sort()
            alpha = (1 - confidence) / 2
            boot = (_percentile(draws, alpha), _percentile(draws, 1 - alpha))
    wilson = wilson_score_interval(num, den, confidence=confidence)
    return Estimate(num, den, len(pairs), num / den, wilson, boot)


@dataclass
class RecallMetricsReport:
    confidence: float
    per_task: list[TaskScore]
    aggregate: dict[str, Estimate]
    opened_without_push: int
    pushed_total: int
    linked_coverage: Estimate
    unobserved_tasks: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Counts, rates and gold-set task ids only (no transcript content)."""
        return {
            "v": 1,
            "confidence": self.confidence,
            "n_tasks": len(self.per_task),
            "unobserved_tasks": list(self.unobserved_tasks),
            "aggregate": {k: v.to_dict() for k, v in self.aggregate.items()},
            "linked_page_push_coverage": self.linked_coverage.to_dict(),
            "opened_without_push": self.opened_without_push,
            "pushed_total": self.pushed_total,
            "per_task": [
                {
                    "task_id": s.task_id,
                    **{m: s.rate(m) for m in METRIC_NAMES},
                    "opened_without_push": s.opened_without_push,
                    "pushed_total": s.pushed_total,
                }
                for s in self.per_task
            ],
        }


def score(
    gold: GoldSet,
    observations: Iterable[TaskObservation],
    *,
    confidence: float = DEFAULT_CONFIDENCE_LEVEL,
    resamples: int = DEFAULT_BOOTSTRAP_RESAMPLES,
    seed: int = DEFAULT_BOOTSTRAP_SEED,
    unobserved: Sequence[str] = (),
) -> RecallMetricsReport:
    """Score each observed task, then aggregate. Tasks without an observation
    are left out (and named in ``unobserved_tasks``), never scored as zero."""
    by_id = {t.task_id: t for t in gold.tasks}
    per_task: list[TaskScore] = []
    for obs in observations:
        if obs.task_id not in by_id:
            raise ValueError(f"observation for unknown task {obs.task_id!r}")
        per_task.append(score_task(by_id[obs.task_id], obs))
    scored_ids = {s.task_id for s in per_task}
    missing = sorted(set(unobserved) | (set(by_id) - scored_ids))
    aggregate = {
        m: _estimate(
            [s._ratio(m) for s in per_task], confidence=confidence, resamples=resamples, seed=seed
        )
        for m in METRIC_NAMES
    }
    linked = _estimate(
        [(s.relevant_linked_pushed, s.relevant_linked_only) for s in per_task],
        confidence=confidence,
        resamples=resamples,
        seed=seed,
    )
    return RecallMetricsReport(
        confidence=confidence,
        per_task=per_task,
        aggregate=aggregate,
        opened_without_push=sum(s.opened_without_push for s in per_task),
        pushed_total=sum(s.pushed_total for s in per_task),
        linked_coverage=linked,
        unobserved_tasks=missing,
    )


def score_from_ledger(
    gold: GoldSet,
    *,
    cache_dir: Path | None = None,
    projects_root: Path | None = None,
    **kwargs: Any,
) -> RecallMetricsReport:
    """Observe every session-bound task, then :func:`score` them."""
    observations, skipped = observe_gold_set(
        gold, cache_dir=cache_dir, projects_root=projects_root
    )
    return score(gold, observations, unobserved=skipped, **kwargs)
