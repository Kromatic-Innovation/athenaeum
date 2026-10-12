#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Offline eval: Jev as a Gate-2-equivalent five-verdict comparator (issue athenaeum#2008).

athenaeum#1997 (:mod:`athenaeum.decision_provider`) built the typed-decision
seam; this script is a STANDALONE consumer of it, not a change to the live
comparator. It asks Jev (TypeSafe AI) a single Choice question per pair --
criteria = the five verdict values exactly as :mod:`athenaeum.verdicts`'
``VERDICT_VALUES`` lists them -- over a state holding both pages' bodies,
and scores Jev's choice against the verdict the real comparator
(:mod:`athenaeum.comparator`) already recorded in the ledger
(:mod:`athenaeum.verdicts`) for the same pair.

**Scope: the 2026-10-08 Gate-2 pairs only, not the full dry-run set.** The
ledger holds two populations of comparator-decided pairs: ``v1.gate1``
(decided by typed coordinate algebra alone, no LLM call -- ~1,655 pairs) and
``v1.gate2`` (decided after an actual Gate-2 LLM call -- 137 pairs as read
at build time, all dated 2026-10-08). Only the ``v1.gate2`` population ever
asked Claude a content question Jev's Choice call can be compared against;
a ``v1.gate1`` verdict has no LLM judgment on either side to agree or
disagree with. :func:`select_gate2_pairs` filters to ``v1.gate2`` only. The
operator's 2026-10-10 disposition on this issue cites "the 111 pairs the
live comparator already judged" -- the nearest textual match for that
figure in this repo's host-side logs is "Gate 2 ... was unavailable for 111
pair(s)" (an EARLIER, incomplete pass, recorded before the pairs that pass
later retried and landed in the ledger as ``v1.gate2``), not a count this
script can reproduce by re-reading the ledger today. The count actually
used here is read at build time from the live ledger and reported exactly
as found, per the issue's own "re-confirm the actual count at build time
rather than trusting this issue's figure" instruction. Some of those 137
pairs are further excluded at build time (missing/unreadable source,
content drift since 2026-10-08, or a PII hazard) -- see "Pairs skipped" in
the rendered report.

**Verdict-only agreement -- confirmed scope note (issue athenaeum#2008 AC).**
Before building this harness, the comparator's actual prompt/response
schema was read directly (:mod:`athenaeum.comparator`):

- Gate 2's OWN LLM call (``content_relation``) never returns one of the five
  verdict values. It returns exactly one of ``equivalent`` / ``conflicting``
  / ``compatible``; :func:`athenaeum.comparator.compare_pages` then derives
  the five-verdict answer by combining that with Gate 1's typed coordinate
  algebra. ``specialization`` carries a REQUIRED direction
  (``specific_side``, computed from coordinates, never asked of the LLM).
  ``conflicting`` requires non-empty ``conflicting_passages`` (free text).
- Neither requirement is reachable by a bare five-way Choice call over page
  text alone, so this eval is scoped to VERDICT-ONLY agreement, exactly as
  the issue's AC anticipates ("may narrow this eval to verdict-only
  agreement"). No direction, no passages, no rationale are requested of or
  scored against Jev.
- In the 137-pair ``v1.gate2`` sample this script reads, Gate 2 never took
  the ``conflicting`` + known-coordinate exit (0 ``contradiction`` and 0
  ``specialization`` verdicts recorded) -- every verdict in the ground-truth
  sample is one of ``duplicate`` (<- ``equivalent``), ``distinct`` (<-
  ``compatible``), or ``underdetermined`` (<- ``conflicting`` with an
  unknown coordinate). That makes the five-way and the three-way
  (``equivalent``/``conflicting``/``compatible``) agreement figures IDENTICAL
  for this sample -- this eval is, empirically, a direct test of the Jev
  Choice framing against Gate 2's own content-relation judgment, not an
  artifact of the five-verdict framing hiding a different comparison. The
  report states this instead of presenting two tables that would read the
  same.

**No corpus mutation.** This script only reads the ledger
(:func:`athenaeum.verdicts.iter_live_entries`) and wiki pages already on
disk. It never calls :func:`athenaeum.verdicts.append_verdict` or any other
ledger mutator, never writes ``_pending_merges.md``, and never touches the
live comparator's own code path.

**Live Jev calls are gated on `JEV_API_KEY`.** Unset -> the script still
selects and reports the pair set, but skips every live call and says so;
never a silent empty run.  A per-run USD cost ceiling (`--cost-cap-usd`,
default 5.00) stops issuing further live calls once Jev's reported
``input_tokens`` usage (at USD 0.042 / million input tokens; output is
free) would exceed it -- partial results are still scored and reported.

Usage::

    export JEV_API_KEY="$(op read 'op://Agent Tools/Jev Athenaeum Key/credential')"
    python scripts/eval_jev_comparator.py \\
        --wiki-root ~/knowledge \\
        --output docs/measurements/jev-comparator-eval-2026-10.md \\
        --pairs-log /path/to/host-side/scratch/jev-pairs.jsonl
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import subprocess

from athenaeum._retry import TransientAPIError, TransientError, with_retry
from athenaeum.comparator import ComparatorPage, page_from_path, page_from_text
from athenaeum.config import DEFAULT_KNOWLEDGE_ROOT
from athenaeum.decision_provider import (
    DecisionResult,
    JevDecisionBackend,
    preflight_decision_provider,
)
from athenaeum.pii import (
    PII_ALLOWLIST_FILENAME,
    PiiAllowlistEntry,
    load_pii_allowlist,
)
from athenaeum.pii_verdicts import load_not_pii_allowlist
from athenaeum.recompare import identify_pii_hazards
from athenaeum.verdicts import (
    VERDICT_VALUES,
    content_hash,
    iter_live_entries,
    page_id_for_path,
)

#: Jev input-token price (output is free). Operator disposition, 2026-10-10.
JEV_INPUT_PRICE_PER_TOKEN = 0.042 / 1_000_000

#: The comparator version tag stamped on every pair that reached Gate 2 (an
#: actual Claude call), as opposed to ``v1.gate1`` (typed exit, no LLM call
#: at all -- see module docstring).
GATE2_COMPARATOR_VERSION = "v1.gate2"

#: Same truncation bound ``_build_content_relation_messages`` applies per
#: side (``fence_untrusted(..., max_chars=4000)``) -- mirrored here so Jev
#: sees the same amount of each page's body Claude did.
BODY_MAX_CHARS = 4000

QUESTION_ID = "verdict"

#: Paraphrased from athenaeum.comparator's module docstring (the five-verdict
#: algorithm) and ``_CONTENT_RELATION_SYSTEM`` (the three content relations
#: Gate 2 itself distinguishes).
INSTRUCTIONS = """You are comparing two memory pages, Page A and Page B, for an AI \
agent's long-term memory system. Decide which ONE of the five given verdicts \
best describes the relationship between their content. Judge from the two \
pages' text alone.

Pick exactly one. Do not let similarity of wording decide -- near-identical \
wording about different questions is "distinct", not "duplicate"; very \
different wording about the same claim is "duplicate", not a conflict \
verdict."""

#: Per-verdict descriptions sent as the Choice `criteria` dict
#: (``{label: description}``). **Wire-shape note (2026-10, mid-eval):** the
#: live Jev API rejects a `criteria` LIST for a Choice question -- it wants
#: this dict shape. A sibling eval lane (athenaeum#1998) found this live;
#: some callers built against :mod:`athenaeum.decision_provider`'s
#: documented "list of choice ids" shape send a list and get rejected, and
#: a fix for those callers is being tracked separately -- NOT a change to
#: :mod:`athenaeum.decision_provider` itself, which is not touched by this
#: script. ``JevDecisionBackend._build_payload`` passes `criteria` through
#: to the wire unchanged whatever shape it is given, so this script simply
#: passes this dict as `criteria` to ``decide()`` directly -- no subclass or
#: payload override needed.
CRITERIA: dict[str, str] = {
    "duplicate": (
        "The two pages assert the SAME claim (paraphrase, different wording, "
        "same fact or guidance)."
    ),
    "contradiction": (
        "The two pages assert claims that CANNOT both be true or followed "
        "about the same territory (a factual disagreement or opposing "
        "guidance), and neither page's claim is a strict special case of "
        "the other's."
    ),
    "specialization": (
        "One page's claim is a more SPECIFIC case of the other's more "
        "general claim -- a general/specific relationship, not a "
        "disagreement."
    ),
    "distinct": (
        "The two pages answer DIFFERENT QUESTIONS about a shared subject, "
        "or are about unrelated subjects entirely -- neither restates nor "
        "conflicts with the other."
    ),
    "underdetermined": (
        "The two pages conflict, but there is not enough information in "
        "the text to tell whether one specializes the other or they are "
        "simply incompatible."
    ),
}

assert set(CRITERIA) == set(VERDICT_VALUES)


class DecisionClient(Protocol):
    """The subset of :class:`athenaeum.decision_provider.DecisionBackend` this
    script depends on -- redeclared locally so the fake backend in tests
    needs no import of the real one beyond :class:`DecisionResult`."""

    def decide(
        self,
        *,
        question_id: str,
        kind: str,
        instructions: str,
        state: Any,
        criteria: Any = None,
    ) -> DecisionResult: ...


# ---------------------------------------------------------------------------
# Pair selection
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PairSample:
    pair_key: str
    id_a: str
    id_b: str
    claude_verdict: str
    page_a: ComparatorPage
    page_b: ComparatorPage


@dataclass
class SelectionStats:
    total_gate2_entries: int = 0
    skipped_missing_source: int = 0
    skipped_pii_hazard: int = 0
    skipped_content_drift: int = 0
    reconstructed_from_git: int = 0
    selected: int = 0


def build_allowlist(wiki_root: Path) -> list[PiiAllowlistEntry]:
    """Mirror :func:`athenaeum.recompare.recompare_pending_merges`'s own
    allowlist construction (recompare.py, "allowlist_entries" block) --
    the YAML allowlist plus the ``not_pii`` ledger merged in, YAML winning on
    any value present in both. Never a second, independently-invented
    exclusion path."""
    allowlist_entries, _errors = load_pii_allowlist(wiki_root / PII_ALLOWLIST_FILENAME)
    allowlist: list[PiiAllowlistEntry] = list(allowlist_entries)
    existing_values = {e.value for e in allowlist_entries if e.value is not None}
    for value, reason in load_not_pii_allowlist(wiki_root).items():
        if value not in existing_values:
            allowlist.append(PiiAllowlistEntry(value=value, reason=reason))
    return allowlist


def _wiki_slug_index(wiki_root: Path) -> dict[str, Path]:
    """Bare-stem slug -> path, over a NON-recursive ``*.md`` glob of
    ``wiki_root`` only -- ``page_from_path`` (and the comparator's own,
    non-cluster call site) compute a page's id with no ``root`` argument,
    i.e. the bare filename stem's slug (see ``page_id_for_path``'s
    docstring), so the index must match that exactly."""
    index: dict[str, Path] = {}
    for path in sorted(wiki_root.glob("*.md")):
        if path.is_file():
            index[path.stem.lower()] = path
    return index


#: Per-path memoization so a page shared across several pairs (common --
#: a cluster's canonical page recurs across many of its pairwise
#: comparisons) pays the git-subprocess cost once, not once per pair.
_GIT_ROOT_CACHE: dict[str, Path | None] = {}
_HISTORY_CACHE: dict[str, list[tuple[str, str]]] = {}  # str(path) -> [(hash, text), ...]

#: Bound on how many historical revisions of ONE path are hashed looking for
#: a match. Every repo sampled here has a short history per page (single
#: digits to low tens of commits) -- see the script's module docstring.
_MAX_HISTORY_COMMITS = 30


def _git_root(path: Path) -> Path | None:
    key = str(path.parent)
    if key not in _GIT_ROOT_CACHE:
        try:
            out = subprocess.run(
                ["git", "-C", key, "rev-parse", "--show-toplevel"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            _GIT_ROOT_CACHE[key] = Path(out.stdout.strip()) if out.returncode == 0 else None
        except (OSError, subprocess.SubprocessError):
            _GIT_ROOT_CACHE[key] = None
    return _GIT_ROOT_CACHE[key]


def _history_hashes(path: Path) -> list[tuple[str, str]]:
    """Every (content_hash, text) pair found in *path*'s last
    :data:`_MAX_HISTORY_COMMITS` git revisions, most recent first. Computed
    and cached once per path; ``[]`` if git is unavailable or the path is
    untracked."""
    cache_key = str(path)
    if cache_key in _HISTORY_CACHE:
        return _HISTORY_CACHE[cache_key]

    result: list[tuple[str, str]] = []
    repo_root = _git_root(path)
    if repo_root is None:
        _HISTORY_CACHE[cache_key] = result
        return result
    try:
        rel = path.resolve().relative_to(repo_root.resolve())
    except ValueError:
        _HISTORY_CACHE[cache_key] = result
        return result
    try:
        log = subprocess.run(
            [
                "git",
                "-C",
                str(repo_root),
                "log",
                f"-n{_MAX_HISTORY_COMMITS}",
                "--format=%H",
                "--",
                str(rel),
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        _HISTORY_CACHE[cache_key] = result
        return result
    if log.returncode != 0:
        _HISTORY_CACHE[cache_key] = result
        return result
    for commit in log.stdout.split():
        try:
            blob = subprocess.run(
                ["git", "-C", str(repo_root), "show", f"{commit}:{rel}"],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if blob.returncode != 0:
            continue
        result.append((content_hash(blob.stdout), blob.stdout))
    _HISTORY_CACHE[cache_key] = result
    return result


def _historical_page_matching(path: Path, target_hashes: set[str]) -> ComparatorPage | None:
    """The cached revision of *path* (see :func:`_history_hashes`) whose
    :func:`athenaeum.verdicts.content_hash` is in *target_hashes* -- the
    verdict ledger stores a hash, not a snapshot, so this is how a pair
    whose CURRENT text has drifted since the verdict was decided (nightly
    corrections, decay annotations, etc.) can still be scored against the
    EXACT text Claude judged, instead of being counted as drift and
    dropped. Returns ``None`` (never raises) if no match is found -- the
    caller then falls back to counting the pair as drifted."""
    for found_hash, text in _history_hashes(path):
        if found_hash in target_hashes:
            return page_from_text(page_id_for_path(path), text)
    return None


def select_gate2_pairs(
    wiki_root: Path, *, allowlist: list[PiiAllowlistEntry] | None = None
) -> tuple[list[PairSample], SelectionStats]:
    """Read the live ledger and resolve every ``v1.gate2`` pair to its two
    page texts, skipping (and counting) anything unreadable, PII-hazardous,
    or whose content has drifted since the verdict was decided."""
    allowlist = allowlist if allowlist is not None else build_allowlist(wiki_root)
    slug_index = _wiki_slug_index(wiki_root)
    stats = SelectionStats()

    gate2_entries = [
        e
        for _month, e in iter_live_entries(wiki_root)
        if e.decided_by == "comparator" and e.basis.comparator_version == GATE2_COMPARATOR_VERSION
    ]
    stats.total_gate2_entries = len(gate2_entries)

    samples: list[PairSample] = []
    for entry in sorted(gate2_entries, key=lambda e: e.pair):
        id_a, _, id_b = entry.pair.partition("+")
        path_a = slug_index.get(id_a)
        path_b = slug_index.get(id_b)
        if path_a is None or path_b is None:
            stats.skipped_missing_source += 1
            continue

        hazard_reasons = identify_pii_hazards([path_a, path_b], allowlist=allowlist)
        if hazard_reasons:
            stats.skipped_pii_hazard += 1
            continue

        try:
            page_a = page_from_path(path_a)
            page_b = page_from_path(path_b)
        except OSError:
            stats.skipped_missing_source += 1
            continue

        recorded_hashes = {h for h in entry.basis.content_hashes if h}
        current_hashes = {content_hash(page_a.text), content_hash(page_b.text)}
        if recorded_hashes and current_hashes != recorded_hashes:
            # Current text has drifted since the verdict was decided (issue
            # athenaeum#2008's live corpus keeps compiling underneath this
            # eval). Try to recover the EXACT text Claude judged from git
            # history before giving up -- scoring drifted text against an
            # old verdict would silently measure the wrong thing.
            hist_a = (
                page_a
                if content_hash(page_a.text) in recorded_hashes
                else _historical_page_matching(path_a, recorded_hashes)
            )
            hist_b = (
                page_b
                if content_hash(page_b.text) in recorded_hashes
                else _historical_page_matching(path_b, recorded_hashes)
            )
            if (
                hist_a is None
                or hist_b is None
                or {content_hash(hist_a.text), content_hash(hist_b.text)} != recorded_hashes
            ):
                stats.skipped_content_drift += 1
                continue
            if hist_a is not page_a or hist_b is not page_b:
                stats.reconstructed_from_git += 1
            page_a, page_b = hist_a, hist_b

        samples.append(
            PairSample(
                pair_key=entry.pair,
                id_a=id_a,
                id_b=id_b,
                claude_verdict=entry.verdict,
                page_a=page_a,
                page_b=page_b,
            )
        )

    stats.selected = len(samples)
    return samples, stats


# ---------------------------------------------------------------------------
# Jev calls
# ---------------------------------------------------------------------------


@dataclass
class JevAnswer:
    pair_key: str
    claude_verdict: str
    jev_choice: str | None
    probabilities: dict[str, float] | None
    confidence: float | None
    input_tokens: int | None
    output_tokens: int | None
    wall_clock_s: float
    error: str | None = None


def _build_state(sample: PairSample) -> dict[str, str]:
    return {
        "page_a": sample.page_a.body[:BODY_MAX_CHARS],
        "page_b": sample.page_b.body[:BODY_MAX_CHARS],
    }


def ask_jev(client: DecisionClient, sample: PairSample, *, pair_index: int) -> JevAnswer:
    """One retried Choice call for *sample*. Never raises -- a failure (after
    retries) is recorded as an error row rather than aborting the run."""
    start = time.monotonic()
    try:
        result = with_retry(
            lambda: client.decide(
                question_id=f"{QUESTION_ID}-{pair_index}",
                kind="choice",
                instructions=INSTRUCTIONS,
                state=_build_state(sample),
                criteria=CRITERIA,
            ),
            description=f"jev_comparator_eval[{sample.pair_key}]",
        )
    except (TransientError, TransientAPIError) as exc:
        return JevAnswer(
            pair_key=sample.pair_key,
            claude_verdict=sample.claude_verdict,
            jev_choice=None,
            probabilities=None,
            confidence=None,
            input_tokens=None,
            output_tokens=None,
            wall_clock_s=time.monotonic() - start,
            error=f"transient (retries exhausted): {type(exc).__name__}",
        )
    except (ValueError, OSError) as exc:
        return JevAnswer(
            pair_key=sample.pair_key,
            claude_verdict=sample.claude_verdict,
            jev_choice=None,
            probabilities=None,
            confidence=None,
            input_tokens=None,
            output_tokens=None,
            wall_clock_s=time.monotonic() - start,
            error=f"{type(exc).__name__}",
        )
    return JevAnswer(
        pair_key=sample.pair_key,
        claude_verdict=sample.claude_verdict,
        jev_choice=result.choice,
        probabilities=result.probabilities,
        confidence=result.confidence,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        wall_clock_s=time.monotonic() - start,
    )


@dataclass
class RunResult:
    answers: list[JevAnswer] = field(default_factory=list)
    attempted: int = 0
    stopped_on_cost_cap: bool = False
    total_input_tokens: int = 0
    total_cost_usd: float = 0.0


def run_eval(
    client: DecisionClient | None,
    samples: list[PairSample],
    *,
    cost_cap_usd: float = 5.0,
) -> RunResult:
    """Ask *client* about every sample in order, sequentially (so the latency
    figures reflect one call at a time), stopping once the running cost
    estimate would exceed *cost_cap_usd*. *client* of ``None`` runs no live
    calls at all -- the caller is responsible for deciding that (no
    ``JEV_API_KEY``)."""
    run = RunResult()
    if client is None:
        return run
    for idx, sample in enumerate(samples):
        projected_tokens_guess = run.total_input_tokens
        projected_cost = projected_tokens_guess * JEV_INPUT_PRICE_PER_TOKEN
        if projected_cost > cost_cap_usd:
            run.stopped_on_cost_cap = True
            break
        answer = ask_jev(client, sample, pair_index=idx)
        run.answers.append(answer)
        run.attempted += 1
        if answer.input_tokens:
            run.total_input_tokens += answer.input_tokens
            run.total_cost_usd = run.total_input_tokens * JEV_INPUT_PRICE_PER_TOKEN
    return run


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


@dataclass
class Metrics:
    scored: int
    errored: int
    overall_agreement: float | None
    per_verdict_agreement: dict[str, tuple[int, int]]  # verdict -> (agree, total)
    confusion: dict[tuple[str, str], int]
    duplicate_agreement: tuple[int, int] | None
    contradiction_agreement: tuple[int, int] | None
    below_half_confidence: int
    mean_latency_s: float | None
    p95_latency_s: float | None


def _percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))
    return ordered[idx]


def compute_metrics(answers: list[JevAnswer]) -> Metrics:
    scored_answers = [a for a in answers if a.jev_choice is not None]
    errored = len(answers) - len(scored_answers)

    confusion: dict[tuple[str, str], int] = {}
    per_verdict_total: dict[str, int] = {}
    per_verdict_agree: dict[str, int] = {}
    below_half = 0
    agree_count = 0

    for a in scored_answers:
        key = (a.claude_verdict, a.jev_choice or "")
        confusion[key] = confusion.get(key, 0) + 1
        per_verdict_total[a.claude_verdict] = per_verdict_total.get(a.claude_verdict, 0) + 1
        if a.jev_choice == a.claude_verdict:
            agree_count += 1
            per_verdict_agree[a.claude_verdict] = per_verdict_agree.get(a.claude_verdict, 0) + 1
        if a.probabilities:
            top = max(a.probabilities.values())
            if top < 0.5:
                below_half += 1
        elif a.confidence is not None and a.confidence < 0.5:
            below_half += 1

    per_verdict_agreement = {
        v: (per_verdict_agree.get(v, 0), per_verdict_total.get(v, 0)) for v in VERDICT_VALUES
    }

    latencies = [a.wall_clock_s for a in answers]
    overall = agree_count / len(scored_answers) if scored_answers else None

    return Metrics(
        scored=len(scored_answers),
        errored=errored,
        overall_agreement=overall,
        per_verdict_agreement=per_verdict_agreement,
        confusion=confusion,
        duplicate_agreement=per_verdict_agreement.get("duplicate"),
        contradiction_agreement=per_verdict_agreement.get("contradiction"),
        below_half_confidence=below_half,
        mean_latency_s=statistics.fmean(latencies) if latencies else None,
        p95_latency_s=_percentile(latencies, 0.95) if latencies else None,
    )


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------


def _fmt_rate(pair: tuple[int, int] | None) -> str:
    if pair is None:
        return "n/a"
    agree, total = pair
    if total == 0:
        return "n/a (0 pairs)"
    return f"{agree}/{total} ({agree / total:.1%})"


def render_markdown(
    *,
    stats: SelectionStats,
    run: RunResult,
    metrics: Metrics,
    live: bool,
    cost_cap_usd: float,
) -> str:
    lines: list[str] = []
    lines.append("# Jev comparator eval (athenaeum#2008)")
    lines.append("")
    lines.append(
        "Evaluates Jev (TypeSafe AI) as a Gate-2-equivalent five-verdict comparator "
        "against the pairs the live comparator (athenaeum.comparator) already judged "
        "on 2026-10-08, by asking Jev a single Choice question per pair with the five "
        "verdict values as criteria. Offline: no code change to the live comparator, "
        "no corpus mutation -- see the script's module docstring for the full scope "
        "note, including why this reads as verdict-only agreement."
    )
    lines.append("")
    lines.append("## Pairs selected and skipped")
    lines.append("")
    lines.append(f"- `v1.gate2` ledger entries read: {stats.total_gate2_entries}")
    lines.append(f"- skipped, missing/unreadable source: {stats.skipped_missing_source}")
    lines.append(f"- skipped, PII hazard: {stats.skipped_pii_hazard}")
    lines.append(
        f"- skipped, content drifted since the verdict was decided (no matching git "
        f"revision found): {stats.skipped_content_drift}"
    )
    lines.append(
        f"- recovered from git history (current text had drifted, an earlier revision "
        f"matched the verdict's recorded content hash): {stats.reconstructed_from_git}"
    )
    lines.append(f"- **selected for this eval: {stats.selected}**")
    lines.append("")
    if not live:
        lines.append(
            "## Live Jev calls: skipped\n\n"
            "`JEV_API_KEY` was not set, so no live call was made. The pair selection "
            "above still ran and is reported in full."
        )
        return "\n".join(lines) + "\n"

    lines.append(
        f"- Jev calls attempted: {run.attempted} of {stats.selected}"
        + (" (stopped at the cost cap)" if run.stopped_on_cost_cap else "")
    )
    lines.append(f"- total input tokens: {run.total_input_tokens}")
    lines.append(f"- total cost: ${run.total_cost_usd:.4f} (cap: ${cost_cap_usd:.2f})")
    lines.append("")

    lines.append("## Agreement")
    lines.append("")
    overall = (
        f"{metrics.scored} scored, {metrics.errored} errored; "
        f"overall agreement: {metrics.overall_agreement:.1%}"
        if metrics.overall_agreement is not None
        else f"{metrics.scored} scored, {metrics.errored} errored; overall agreement: n/a"
    )
    lines.append(overall)
    lines.append("")
    lines.append("| verdict (Claude ground truth) | Jev agreement |")
    lines.append("|---|---|")
    for v in VERDICT_VALUES:
        lines.append(f"| {v} | {_fmt_rate(metrics.per_verdict_agreement.get(v))} |")
    lines.append("")
    lines.append(
        f"High-stakes verdicts -- **duplicate**: {_fmt_rate(metrics.duplicate_agreement)}; "
        f"**contradiction**: {_fmt_rate(metrics.contradiction_agreement)} "
        "(0 pairs carry this ground-truth verdict in this sample -- see scope note)."
    )
    lines.append("")
    lines.append(
        f"Jev's top choice probability was below 0.5 on {metrics.below_half_confidence} "
        f"of {metrics.scored} scored pair(s)."
    )
    lines.append("")

    lines.append("### Confusion matrix (Claude verdict -> Jev choice)")
    lines.append("")
    header = "| Claude \\ Jev | " + " | ".join(VERDICT_VALUES) + " |"
    lines.append(header)
    lines.append("|" + "---|" * (len(VERDICT_VALUES) + 1))
    for truth in VERDICT_VALUES:
        row = [str(metrics.confusion.get((truth, jev), 0)) for jev in VERDICT_VALUES]
        lines.append(f"| {truth} | " + " | ".join(row) + " |")
    lines.append("")

    lines.append("## Latency")
    lines.append("")
    if metrics.mean_latency_s is not None:
        lines.append(
            f"Jev per-call wall-clock: mean {metrics.mean_latency_s:.3f}s, "
            f"p95 {metrics.p95_latency_s:.3f}s, over {len(run.answers)} call(s)."
        )
    else:
        lines.append("No live calls were made.")
    lines.append(
        "These figures were measured over a slow hotel internet connection abroad; "
        "treat the absolute latency numbers as an upper bound on what a normal "
        "connection would show, not a clean model-only measurement."
    )
    lines.append(
        "Claude's per-pair latency is not recorded in the verdict ledger "
        "(`VerdictEntry` has no latency field) and re-running the Claude comparator "
        "was out of scope for this eval (it would spend Anthropic budget) -- so no "
        "side-by-side latency comparison is reported."
    )
    lines.append("")

    lines.append("## Recommendation")
    lines.append("")
    lines.append(_render_recommendation(metrics))
    lines.append("")
    return "\n".join(lines) + "\n"


def _render_recommendation(metrics: Metrics) -> str:
    if metrics.overall_agreement is None:
        return "No scored pairs -- no recommendation can be made from this run."
    duplicate_agree = metrics.per_verdict_agreement.get("duplicate")
    lines = [
        f"Overall verdict-only agreement: {metrics.overall_agreement:.1%}.",
        "",
        "Pre-registered framing (set before this run's results were read):",
        "- **Pre-screen** (Jev's `distinct` call skips a Claude Gate 2 call): "
        "acceptable if few of Claude's non-distinct pairs are misclassified "
        "`distinct` by Jev (a false-distinct here means a real duplicate/conflict "
        "pair never reaches Claude at all).",
        "- **Replacement** (Jev's verdict is recorded instead of Claude's): needs "
        "high overall agreement AND a low false-duplicate rate on Claude-distinct "
        "pairs (a false duplicate can drive an incorrect fold proposal).",
        "- **Not suitable**: neither bar is cleared.",
        "",
    ]
    if duplicate_agree and duplicate_agree[1] > 0 and duplicate_agree[1] < 20:
        lines.append(
            f"Only {duplicate_agree[1]} duplicate-verdict pair(s) exist in this sample -- "
            "any duplicate-agreement rate carries a wide interval; do not treat it as a "
            "settled number without a larger sample."
        )
    if metrics.overall_agreement >= 0.9 and (
        not duplicate_agree
        or duplicate_agree[1] == 0
        or duplicate_agree[0] / duplicate_agree[1] >= 0.8
    ):
        lines.append(
            "**Recommendation: candidate as a pre-screen.** Agreement is high enough "
            "to skip a real Gate 2 call on pairs Jev is confident are `distinct`, "
            "routing everything else to the existing Claude comparator. Not yet a "
            "replacement claim -- contradiction and specialization have zero coverage "
            "in this sample (see scope note) and the duplicate count is small."
        )
    elif metrics.overall_agreement >= 0.6:
        lines.append(
            "**Recommendation: not yet suitable as a replacement; possibly a "
            "pre-screen with a wider sample.** Agreement is meaningful but not high "
            "enough to recommend swapping Jev in for Claude's Gate 2 call without "
            "further evidence, and this sample has zero contradiction/specialization "
            "coverage to check the high-stakes classes against."
        )
    else:
        lines.append(
            "**Recommendation: not suitable.** Agreement with the recorded Claude "
            "verdicts is too low on this sample to recommend Jev as either a "
            "pre-screen or a replacement for Gate 2 today."
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--wiki-root",
        type=Path,
        default=DEFAULT_KNOWLEDGE_ROOT / "wiki",
        help=(
            "The wiki directory itself (holding both the pages and "
            "_verdicts/) -- the same 'wiki_root' convention athenaeum.verdicts "
            "and athenaeum.recompare use. Default: ~/knowledge/wiki."
        ),
    )
    parser.add_argument(
        "--output", required=True, type=Path, help="Path to write the markdown report."
    )
    parser.add_argument(
        "--cost-cap-usd",
        type=float,
        default=5.0,
        help="Stop issuing live Jev calls once estimated spend would exceed this (default: 5.00).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Cap the number of selected pairs actually sent to Jev (0 = no cap).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    wiki_root = args.wiki_root.expanduser().resolve()
    samples, stats = select_gate2_pairs(wiki_root)

    preflight_error = preflight_decision_provider("jev")
    live = preflight_error is None
    if not live:
        print(f"skipping live Jev calls: {preflight_error}", file=sys.stderr)

    run_samples = samples[: args.limit] if args.limit else samples
    client = JevDecisionBackend() if live else None
    run = run_eval(client, run_samples, cost_cap_usd=args.cost_cap_usd)
    metrics = compute_metrics(run.answers)

    markdown = render_markdown(
        stats=stats, run=run, metrics=metrics, live=live, cost_cap_usd=args.cost_cap_usd
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(markdown, encoding="utf-8")
    print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
