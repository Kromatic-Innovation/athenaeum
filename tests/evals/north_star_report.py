# SPDX-License-Identifier: Apache-2.0
"""North-star report: the free dimensions only (issue athenaeum#1523).

Reads completed rollout rows out of a ``tests.evals.containment.ResultStore``
(one row per ``GridCell(probe, arm, corpus_scale, replicate)``, persisted via
:func:`append_rollout_row` / decoded via :meth:`tests.evals.rollout.RolloutRecord.from_payload`)
and renders the dimensions the issue's dimension table asks for -- and
nothing that needs a judge:

* **PULL no-call rate** -- reported first, per the issue's own framing:
  the thesis under test predicts a non-trivial no-call rate, so it is the
  most direct evidence and is never buried under other sections.
* **Query quality** -- lexical overlap (:func:`lexical_overlap`, a plain
  Jaccard over content-bearing tokens) between a PULL turn's self-authored
  recall query and the probe's ground-truth target page, set beside the
  SAME overlap for a zero-cost "topic-extracted" baseline query. That
  baseline is, deliberately, the probe's own original query text used
  verbatim: it costs nothing to obtain (it already exists -- no extraction
  step, let alone an LLM, is run to produce it) and is exactly what a
  system that skipped query reformulation entirely would have sent.
* **Cost** -- input/output tokens **per turn**
  (``RolloutRecord.turn_tokens``), not summed per task, so PUSH's
  ``injected_context_tokens`` (paid whether the pages were used or not)
  can be read directly beside PULL's turn cost (paid only when it calls) --
  the asymmetry the issue names as the actual economic question.
* **Efficiency** -- turns to answer, tool-call count.
* **Waste** -- delivered pages never cited later in the rollout, both as a
  page-count fraction and as an approximate token figure.
* **Utilization** -- the FREE portion only: uid citation (reusing
  :func:`tests.evals.metrics.uids_from_recall_output`, the same extractor
  ``recall_search``'s own rendered output already round-trips through) and
  distinctive n-gram overlap (:func:`distinctive_ngram_overlap`). The
  judged per-claim support check is explicitly out of scope (issue's own
  acceptance criterion: no LLM judge anywhere in this module).

**Every dimension is broken out per ``probe_class`` x ``corpus_scale``,
never collapsed to a single aggregate** -- the issue's hypothesis is that
*confusability* (``probe_class``), not raw corpus size, degrades retrieval,
and a report that averages across classes cannot test that.

**Cost and quality are reported as a frontier, never a composite.** No
function in this module reduces the dimensions above to one weighted
score -- the weights are a business judgement the issue explicitly reserves
for the reader, so nothing here computes them. Grep for confirmation: no
"score"/"weight"/"composite" field exists on :class:`NorthStarReport` or in
:func:`render_report`'s output.

**No LLM judge is invoked anywhere on this path.** Every function here is a
pure computation over already-captured :class:`~tests.evals.rollout.RolloutRecord`
rows -- nothing in this module imports :mod:`athenaeum.provider`,
:func:`tests.evals.harness.build_live_client`, or spawns ``claude -p``. See
``tests/evals/test_north_star_report.py``'s
``test_no_model_client_constructed_on_report_path`` for the enforcement.

Report writer conventions mirror ``src/athenaeum/shadow_parity.py``'s
``render_report``/``write_report`` (issue athenaeum#1333) rather than
inventing a second convention: dated filename
(``north-star-<YYYY-MM-DD>.md``), a non-clobbering numeric suffix on a
same-day rerun, a provenance stamp (athenaeum version, git SHA, generation
timestamp, per-corpus-scale digest), and a prominent ``PARTIAL`` banner
when the run that produced the rows aborted.

Layering: sits under ``tests/evals/`` (test-only, like
``tests/evals/containment.py``/``tests/evals/rollout.py``) -- no
``src/athenaeum/*.py`` module is added, so no
``tests/fixtures/layer_declarations.py`` entry is needed.
"""

from __future__ import annotations

import dataclasses
import json
import re
from collections import defaultdict
from collections.abc import Sequence
from pathlib import Path

from athenaeum.push_metrics import estimate_tokens
from athenaeum.shadow_linkage import _get_git_sha, _get_version
from athenaeum.store import now_iso
from tests.evals.containment import GridCell, ResultStore
from tests.evals.corpus import Corpus, Probe, build_corpus
from tests.evals.metrics import uids_from_recall_output
from tests.evals.rollout import Arm, RolloutRecord

DEFAULT_MEASUREMENTS_DIR = Path("measurements")

# ---------------------------------------------------------------------------
# Persistence bridge: ResultStore rows <-> RolloutRecord
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class RolloutRow:
    """One decoded result-store row: the ``GridCell`` identity plus the
    ``RolloutRecord`` it produced."""

    cell: GridCell
    record: RolloutRecord


def append_rollout_row(store: ResultStore, cell: GridCell, record: RolloutRecord) -> None:
    """Persist *record* under *cell*'s identity key.

    ``replicate`` is folded into the stored payload alongside
    :meth:`RolloutRecord.to_payload`'s fields -- ``RolloutRecord`` itself
    carries no replicate field (it names ONE (probe, arm, corpus_scale)
    rollout; replicate is the grid's own axis), so the persisted row needs
    it explicitly for :func:`load_rollout_rows` to reconstruct the full
    ``GridCell`` without reparsing ``cell_key``.
    """
    payload = {**record.to_payload(), "replicate": cell.replicate}
    store.append(cell.cell_key(), payload)


def load_rollout_rows(store: ResultStore) -> list[RolloutRow]:
    """Read every persisted row out of *store*, decoded back to
    ``(GridCell, RolloutRecord)`` pairs. Empty list if the store has no
    file yet (a fresh, never-run store)."""
    if not store.path.exists():
        return []
    rows: list[RolloutRow] = []
    with store.path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            raw = json.loads(line)
            cell = GridCell(
                probe=raw["probe_id"],
                arm=raw["arm"],
                corpus_scale=raw["corpus_scale"],
                replicate=int(raw["replicate"]),
            )
            record = RolloutRecord.from_payload(raw)
            rows.append(RolloutRow(cell=cell, record=record))
    return rows


# ---------------------------------------------------------------------------
# Free-to-compute text metrics
# ---------------------------------------------------------------------------

#: A short, deliberately conservative stopword list -- excluded so a
#: lexical/n-gram overlap is not dominated by function words that would
#: overlap between almost any two English passages regardless of topic.
#: NOT a general-purpose NLP stopword list (no external dependency is
#: pulled in for this); just enough to keep the free metrics meaningful.
_STOPWORDS = frozenset(
    {
        "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "is",
        "are", "was", "were", "be", "been", "with", "as", "at", "by", "it",
        "this", "that", "these", "those", "from", "not", "no", "do", "does",
        "did", "what", "which", "who", "how", "when", "where", "why",
    }
)
_WORD_RE = re.compile(r"[a-z0-9]+")


def _content_terms(text: str) -> set[str]:
    words = _WORD_RE.findall(text.lower())
    return {w for w in words if len(w) >= 3 and w not in _STOPWORDS}


def lexical_overlap(a: str, b: str) -> float:
    """Jaccard overlap between the content-term sets of *a* and *b*.

    ``overlap(a, b) = |terms(a) & terms(b)| / |terms(a) | terms(b)|``
    -- ``terms()`` is lowercased alphanumeric tokens of length >= 3, minus
    :data:`_STOPWORDS`. ``0.0`` when either side has no content terms
    (never a division by zero, and never treated as "perfect overlap").
    """
    ta, tb = _content_terms(a), _content_terms(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _ngrams(text: str, n: int) -> set[tuple[str, ...]]:
    words = _WORD_RE.findall(text.lower())
    if len(words) < n:
        return set()
    return {tuple(words[i : i + n]) for i in range(len(words) - n + 1)}


#: Word-shingle size for :func:`distinctive_ngram_overlap`. 4 words is long
#: enough that a shared shingle is very unlikely by chance (unlike a
#: 1- or 2-gram, which overlaps between almost any two passages on the same
#: topic) while still short enough to survive light paraphrase of a
#: sentence fragment.
DISTINCTIVE_NGRAM_SIZE = 4


def distinctive_ngram_overlap(
    delivered: str, answer: str, n: int = DISTINCTIVE_NGRAM_SIZE
) -> float:
    """Fraction of *delivered*'s n-gram shingles that also appear in *answer*.

    ``overlap = |ngrams(delivered) & ngrams(answer)| / |ngrams(delivered)|``
    -- the denominator is the shingle count of the DELIVERED text (what
    there was to draw from), not the answer's, so the metric reads as
    "how much of what was delivered shows up verbatim in the answer",
    never the reverse. ``0.0`` when *delivered* is too short to have any
    n-grams at all (never a division by zero).

    This is a coarse, free, zero-judgment signal that the answer echoes
    delivered phrasing -- it is NOT a claim-support check (that requires a
    judge and is explicitly out of scope for this report).
    """
    delivered_grams = _ngrams(delivered, n)
    if not delivered_grams:
        return 0.0
    answer_grams = _ngrams(answer, n)
    return len(delivered_grams & answer_grams) / len(delivered_grams)


# ---------------------------------------------------------------------------
# Corpus/probe lookups (needed for target-page text + ground-truth uids)
# ---------------------------------------------------------------------------

#: Memoizes ``build_corpus`` by scale -- every rollout row at the same
#: ``corpus_scale`` was generated from the SAME deterministic seed (see
#: ``tests.evals.corpus.build_corpus``'s own docstring), so rebuilding it
#: once per scale per report (rather than once per row) is exact, not an
#: approximation.
_CORPUS_CACHE: dict[str, Corpus] = {}


def _corpus_for_scale(scale: str) -> Corpus:
    if scale not in _CORPUS_CACHE:
        _CORPUS_CACHE[scale] = build_corpus(scale)
    return _CORPUS_CACHE[scale]


def _probe_for_row(row: RolloutRow) -> Probe:
    corpus = _corpus_for_scale(row.record.corpus_scale)
    for probe in corpus.probes:
        if probe.id == row.record.probe_id:
            return probe
    raise KeyError(
        f"probe {row.record.probe_id!r} not found in corpus scale={row.record.corpus_scale!r}"
    )


def _target_page_text(probe: Probe, corpus: Corpus) -> str:
    """The ground-truth pages' rendered text -- what a PULL/PUSH query is
    ultimately trying to retrieve. Empty for an abstention probe (matches
    ``tests.evals.rollout._oracle_context``'s own empty-context rule)."""
    pages_by_uid = {page.uid: page for page in corpus.pages}
    parts = [
        pages_by_uid[uid].to_markdown() for uid in probe.expected_uids if uid in pages_by_uid
    ]
    return "\n\n---\n\n".join(parts)


# ---------------------------------------------------------------------------
# Delivered-content extraction (what the arm actually put in front of the model)
# ---------------------------------------------------------------------------


def _push_delivered_text(record: RolloutRecord) -> str:
    if not record.transcript:
        return ""
    return str(record.transcript[0].get("pushed_context") or "")


def _pull_delivered_text(record: RolloutRecord) -> str:
    """Best-effort extraction of the recall tool's OWN result content from
    the raw ``claude -p --output-format stream-json`` transcript
    (``RolloutRecord.transcript`` -- the full parsed event list,
    ``tests.evals.rollout.parse_pull_stream`` keeps every event verbatim).

    A tool result surfaces as a ``type: "user"`` event whose
    ``message.content`` carries a ``type: "tool_result"`` block; that
    block's own ``content`` is either a plain string or a list of
    ``{"type": "text", "text": ...}`` blocks (the two shapes the Claude
    Code stream-json format uses). Returns ``""`` (never raises) for any
    transcript that does not carry this shape -- including every PULL
    rollout that never called recall in the first place, which is the
    common case this function must handle silently, not exceptionally.
    """
    parts: list[str] = []
    for event in record.transcript:
        if not isinstance(event, dict) or event.get("type") != "user":
            continue
        message = event.get("message")
        if not isinstance(message, dict):
            continue
        for block in message.get("content") or []:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            content = block.get("content")
            if isinstance(content, str):
                parts.append(content)
            elif isinstance(content, list):
                for sub in content:
                    if isinstance(sub, dict) and sub.get("type") == "text":
                        parts.append(str(sub.get("text", "")))
    return "\n\n".join(parts)


def delivered_text_for_utilization(row: RolloutRow) -> str:
    """The text actually placed in front of the model for *row* -- the
    basis for both :func:`distinctive_ngram_overlap` and the delivered-uid
    set below. ``""`` for NONE (nothing delivered) and for a PULL rollout
    that never called recall."""
    record = row.record
    if record.arm is Arm.PUSH:
        return _push_delivered_text(record)
    if record.arm is Arm.ORACLE:
        return _target_page_text(_probe_for_row(row), _corpus_for_scale(record.corpus_scale))
    if record.arm is Arm.PULL:
        return _pull_delivered_text(record)
    return ""


def delivered_uids_for_utilization(row: RolloutRow) -> tuple[str, ...]:
    """Uids of the pages actually delivered for *row*.

    PUSH and PULL are both served by the SAME ``recall_search`` rendering
    (``**Uid:**`` marker), so :func:`~tests.evals.metrics.uids_from_recall_output`
    applies unchanged to either one. ORACLE's context is the ground-truth
    pages verbatim (:func:`tests.evals.rollout._oracle_context`) rendered
    via ``Page.to_markdown()`` -- plain ``uid:`` frontmatter, not the bold
    marker -- so ORACLE uses the probe's own ``expected_uids`` directly
    rather than mis-parsing a format that was never meant to match.
    """
    record = row.record
    if record.arm is Arm.PUSH:
        return tuple(uids_from_recall_output(_push_delivered_text(record)))
    if record.arm is Arm.ORACLE:
        return _probe_for_row(row).expected_uids
    if record.arm is Arm.PULL:
        return tuple(uids_from_recall_output(_pull_delivered_text(record)))
    return ()


def uid_citation_rate(record: RolloutRecord, delivered_uids: Sequence[str]) -> float | None:
    """Fraction of *delivered_uids* whose literal uid string appears in
    *record*'s answer. ``None`` (not ``0.0``) when nothing was delivered --
    "nothing to cite" is a different fact from "delivered but never cited".
    """
    if not delivered_uids:
        return None
    cited = sum(1 for uid in delivered_uids if uid in record.answer)
    return cited / len(delivered_uids)


# ---------------------------------------------------------------------------
# Grouping: per probe_class x corpus_scale x arm -- never a single aggregate
# ---------------------------------------------------------------------------

GroupKey = tuple[str, str, str]  # (probe_class, corpus_scale, arm)


def _group_key(row: RolloutRow) -> GroupKey:
    return (row.record.probe_class, row.record.corpus_scale, row.record.arm.value)


def _group_rows(rows: Sequence[RolloutRow]) -> dict[GroupKey, list[RolloutRow]]:
    groups: dict[GroupKey, list[RolloutRow]] = defaultdict(list)
    for row in rows:
        groups[_group_key(row)].append(row)
    return dict(groups)


@dataclasses.dataclass(frozen=True)
class GroupStats:
    """Every free dimension, computed for one (probe_class, corpus_scale,
    arm) group. Fields are populated where the arm/data makes them
    meaningful and left ``None`` otherwise (see each field's own note) --
    never silently coerced to ``0.0``, which would be indistinguishable
    from a real zero."""

    probe_class: str
    corpus_scale: str
    arm: str
    n: int

    # PULL no-call rate (PULL only; None for other arms)
    no_call_rate: float | None

    # Query quality (PULL, calls only)
    mean_self_query_overlap: float | None
    mean_topic_query_overlap: float | None

    # Cost -- per turn, not per task
    mean_input_tokens_per_turn: float | None
    mean_output_tokens_per_turn: float | None
    mean_injected_context_tokens: float | None  # PUSH/ORACLE only -- paid whether used or not

    # Efficiency
    mean_turn_count: float
    mean_tool_call_count: float

    # Waste
    mean_wasted_page_fraction: float | None
    mean_wasted_tokens_estimate: float | None

    # Utilization (free portion)
    mean_uid_citation_rate: float | None
    mean_distinctive_ngram_overlap: float | None


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def compute_group_stats(rows: Sequence[RolloutRow]) -> list[GroupStats]:
    """Compute :class:`GroupStats` for every (probe_class, corpus_scale,
    arm) group present in *rows*. Sorted for a stable report ordering."""
    stats: list[GroupStats] = []
    for (probe_class, corpus_scale, arm), group in sorted(_group_rows(rows).items()):
        no_call_flags: list[float] = []
        self_overlaps: list[float] = []
        topic_overlaps: list[float] = []
        input_per_turn: list[float] = []
        output_per_turn: list[float] = []
        injected_tokens: list[float] = []
        turn_counts: list[float] = []
        tool_call_counts: list[float] = []
        wasted_fracs: list[float] = []
        wasted_tokens: list[float] = []
        citation_rates: list[float] = []
        ngram_overlaps: list[float] = []

        for row in group:
            record = row.record
            turn_counts.append(float(record.turn_count))
            tool_call_counts.append(float(len(record.tool_calls)))
            for turn in record.turn_tokens:
                input_per_turn.append(float(turn.input_tokens))
                output_per_turn.append(float(turn.output_tokens))

            if record.arm is Arm.PULL:
                no_call_flags.append(0.0 if record.recall_called else 1.0)
                if record.recall_called:
                    probe = _probe_for_row(row)
                    target = _target_page_text(probe, _corpus_for_scale(corpus_scale))
                    for call in record.tool_calls:
                        self_overlaps.append(lexical_overlap(call.query, target))
                    topic_overlaps.append(lexical_overlap(probe.query, target))

            if record.injected_context_tokens is not None:
                injected_tokens.append(float(record.injected_context_tokens))

            delivered_uids = delivered_uids_for_utilization(row)
            delivered_text = delivered_text_for_utilization(row)
            citation = uid_citation_rate(record, delivered_uids)
            if citation is not None:
                citation_rates.append(citation)
                wasted_fracs.append(1.0 - citation)
                if delivered_text:
                    wasted_tokens.append(estimate_tokens(delivered_text) * (1.0 - citation))

            if delivered_text:
                ngram_overlaps.append(distinctive_ngram_overlap(delivered_text, record.answer))

        stats.append(
            GroupStats(
                probe_class=probe_class,
                corpus_scale=corpus_scale,
                arm=arm,
                n=len(group),
                no_call_rate=_mean(no_call_flags) if arm == Arm.PULL.value else None,
                mean_self_query_overlap=_mean(self_overlaps),
                mean_topic_query_overlap=_mean(topic_overlaps),
                mean_input_tokens_per_turn=_mean(input_per_turn),
                mean_output_tokens_per_turn=_mean(output_per_turn),
                mean_injected_context_tokens=_mean(injected_tokens),
                mean_turn_count=_mean(turn_counts) or 0.0,
                mean_tool_call_count=_mean(tool_call_counts) or 0.0,
                mean_wasted_page_fraction=_mean(wasted_fracs),
                mean_wasted_tokens_estimate=_mean(wasted_tokens),
                mean_uid_citation_rate=_mean(citation_rates),
                mean_distinctive_ngram_overlap=_mean(ngram_overlaps),
            )
        )
    return stats


# ---------------------------------------------------------------------------
# Report assembly + rendering + writing
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class NorthStarReport:
    rows: tuple[RolloutRow, ...]
    stats: tuple[GroupStats, ...]
    aborted: bool
    abort_reason: str
    athenaeum_version: str
    git_sha: str
    generated: str
    corpus_digests: dict[str, str]


def build_report(
    rows: Sequence[RolloutRow], *, aborted: bool = False, abort_reason: str = ""
) -> NorthStarReport:
    """Assemble a :class:`NorthStarReport` from decoded result-store rows.

    Pure computation -- constructs no model client and makes no network
    call (see the module docstring's "No LLM judge" note)."""
    scales = sorted({row.record.corpus_scale for row in rows})
    digests = {scale: _corpus_for_scale(scale).fingerprint() for scale in scales}
    return NorthStarReport(
        rows=tuple(rows),
        stats=tuple(compute_group_stats(rows)),
        aborted=aborted,
        abort_reason=abort_reason,
        athenaeum_version=_get_version(),
        git_sha=_get_git_sha(),
        generated=now_iso(),
        corpus_digests=digests,
    )


def _fmt(value: float | None, digits: int = 3) -> str:
    return "n/a" if value is None else f"{value:.{digits}f}"


def render_report(report: NorthStarReport) -> str:
    """Render *report* as markdown, in the issue's own dimension order:
    PULL no-call rate first, then query quality, cost, efficiency, waste,
    utilization -- broken out per (probe_class, corpus_scale, arm), with a
    prominent ``PARTIAL`` banner when ``report.aborted``.
    """
    lines: list[str] = []
    if report.aborted:
        lines.append("> **PARTIAL RUN** — stopped early.")
        lines.append(f"> abort_reason: {report.abort_reason}")
        lines.append("")

    lines.append("# North-star report: free dimensions only (athenaeum#1523)")
    lines.append("")
    lines.append(f"- generated: {report.generated}")
    lines.append(f"- athenaeum_version: {report.athenaeum_version}")
    lines.append(f"- git_sha: {report.git_sha}")
    for scale in sorted(report.corpus_digests):
        lines.append(f"- corpus_digest[{scale}]: {report.corpus_digests[scale]}")
    lines.append(f"- total rollout rows: {len(report.rows)}")
    lines.append("")
    lines.append(
        "No LLM judge is invoked anywhere on this path — every figure below is a "
        "deterministic computation over already-captured rollout transcripts. This is "
        "a **measurement, not a regression gate**; nothing here should ever fail a build."
    )
    lines.append("")

    pull_stats = [s for s in report.stats if s.arm == Arm.PULL.value]

    lines.append("## PULL no-call rate (first-class result)")
    lines.append("")
    lines.append(
        "`no_call_rate = count(recall_called=False) / count(PULL rollouts)`, per "
        "(probe_class, corpus_scale). The thesis under test predicts a non-trivial rate; "
        "a rate near 0.0 across every group means the free data do NOT evidence it."
    )
    lines.append("")
    lines.append("| probe_class | corpus_scale | n | no_call_rate |")
    lines.append("| --- | --- | --- | --- |")
    for s in pull_stats:
        lines.append(f"| {s.probe_class} | {s.corpus_scale} | {s.n} | {_fmt(s.no_call_rate)} |")
    lines.append("")

    lines.append("## Query quality")
    lines.append("")
    lines.append(
        "`lexical_overlap(a, b) = |terms(a) ∩ terms(b)| / |terms(a) ∪ terms(b)|` (Jaccard) — "
        "`terms()` is lowercased alphanumeric tokens of length >= 3, minus a short stopword "
        "list; denominator is the union of both term sets, 0.0 when either side is empty. "
        "`self_query_overlap` is measured between the agent's own recall-tool query and the "
        "target (ground-truth) page text; `topic_query_overlap` is the SAME formula against "
        "the same target, but using the probe's original query text verbatim as a zero-cost "
        "baseline (no extraction step is run to produce it — it already exists)."
    )
    lines.append("")
    lines.append("| probe_class | corpus_scale | n | self_query_overlap | topic_query_overlap |")
    lines.append("| --- | --- | --- | --- | --- |")
    for s in pull_stats:
        lines.append(
            f"| {s.probe_class} | {s.corpus_scale} | {s.n} | "
            f"{_fmt(s.mean_self_query_overlap)} | {_fmt(s.mean_topic_query_overlap)} |"
        )
    lines.append("")

    lines.append("## Cost (per turn, not per task)")
    lines.append("")
    lines.append(
        "`mean_injected_context_tokens` (PUSH/ORACLE) is counted whether the delivered pages "
        "were used or not — that is the asymmetry against PULL, which pays only on a turn "
        "where it actually calls recall. Read the two columns side by side, never in isolation."
    )
    lines.append("")
    lines.append(
        "| probe_class | corpus_scale | arm | n | mean_input_tokens/turn | "
        "mean_output_tokens/turn | mean_injected_context_tokens |"
    )
    lines.append("| --- | --- | --- | --- | --- | --- | --- |")
    for s in report.stats:
        lines.append(
            f"| {s.probe_class} | {s.corpus_scale} | {s.arm} | {s.n} | "
            f"{_fmt(s.mean_input_tokens_per_turn, 1)} | {_fmt(s.mean_output_tokens_per_turn, 1)} | "
            f"{_fmt(s.mean_injected_context_tokens, 1)} |"
        )
    lines.append("")

    lines.append("## Efficiency")
    lines.append("")
    lines.append(
        "| probe_class | corpus_scale | arm | n | mean_turn_count | mean_tool_call_count |"
    )
    lines.append("| --- | --- | --- | --- | --- | --- |")
    for s in report.stats:
        lines.append(
            f"| {s.probe_class} | {s.corpus_scale} | {s.arm} | {s.n} | "
            f"{s.mean_turn_count:.2f} | {s.mean_tool_call_count:.2f} |"
        )
    lines.append("")

    lines.append("## Waste")
    lines.append("")
    lines.append(
        "`wasted_page_fraction = 1 - uid_citation_rate` — the fraction of delivered pages "
        "whose uid never appears in the answer text, i.e. never referenced anywhere later in "
        "the rollout. `wasted_tokens_estimate` distributes the delivered content's estimated "
        "token count evenly across delivered uids and sums the wasted share. `n/a` means "
        "nothing was delivered for that group (no basis to call anything wasted)."
    )
    lines.append("")
    lines.append(
        "| probe_class | corpus_scale | arm | n | wasted_page_fraction | wasted_tokens_estimate |"
    )
    lines.append("| --- | --- | --- | --- | --- | --- |")
    for s in report.stats:
        lines.append(
            f"| {s.probe_class} | {s.corpus_scale} | {s.arm} | {s.n} | "
            f"{_fmt(s.mean_wasted_page_fraction)} | {_fmt(s.mean_wasted_tokens_estimate, 1)} |"
        )
    lines.append("")

    lines.append("## Utilization (free portion only)")
    lines.append("")
    lines.append(
        "`uid_citation_rate` = fraction of delivered uids whose literal uid string appears in "
        "the answer text. `distinctive_ngram_overlap` = "
        "`|ngrams(delivered) ∩ ngrams(answer)| / |ngrams(delivered)|` over 4-word shingles — "
        "denominator is the delivered text's own shingle count. Both are coarse, zero-judgment "
        "proxies for \"did the answer draw on what was delivered\" — the judged per-claim "
        "support check is explicitly out of scope for this report."
    )
    lines.append("")
    lines.append(
        "| probe_class | corpus_scale | arm | n | uid_citation_rate | distinctive_ngram_overlap |"
    )
    lines.append("| --- | --- | --- | --- | --- | --- |")
    for s in report.stats:
        lines.append(
            f"| {s.probe_class} | {s.corpus_scale} | {s.arm} | {s.n} | "
            f"{_fmt(s.mean_uid_citation_rate)} | {_fmt(s.mean_distinctive_ngram_overlap)} |"
        )
    lines.append("")

    lines.append("## Frontier (cost vs. quality — never a single composite)")
    lines.append("")
    lines.append(
        "This report intentionally emits no weighted score combining cost and quality: the "
        "weights are a business judgement, not a measurement (issue athenaeum#1523). Read the "
        "Cost and Query-quality/Utilization sections above side by side, per (probe_class, "
        "corpus_scale), and decide the tradeoff from there — the numbers below are restated "
        "unweighted, one arm's cost beside its utilization, nothing summed across them."
    )
    lines.append("")
    lines.append(
        "| probe_class | corpus_scale | arm | mean_input_tokens/turn | mean_output_tokens/turn | "
        "uid_citation_rate | distinctive_ngram_overlap |"
    )
    lines.append("| --- | --- | --- | --- | --- | --- | --- |")
    for s in report.stats:
        lines.append(
            f"| {s.probe_class} | {s.corpus_scale} | {s.arm} | "
            f"{_fmt(s.mean_input_tokens_per_turn, 1)} | {_fmt(s.mean_output_tokens_per_turn, 1)} | "
            f"{_fmt(s.mean_uid_citation_rate)} | {_fmt(s.mean_distinctive_ngram_overlap)} |"
        )
    lines.append("")

    lines.append("## Verdict: do the free dimensions settle the question?")
    lines.append("")
    if not pull_stats:
        lines.append(
            "No PULL rows are present in this run — the free dimensions cannot be evaluated "
            "at all; judged scoring is not reachable from this report either."
        )
    else:
        overall_no_call = _mean([s.no_call_rate for s in pull_stats if s.no_call_rate is not None])
        gaps = [
            (s.mean_self_query_overlap - s.mean_topic_query_overlap)
            for s in pull_stats
            if s.mean_self_query_overlap is not None and s.mean_topic_query_overlap is not None
        ]
        overall_gap = _mean([abs(g) for g in gaps])
        if (overall_no_call or 0.0) <= 0.0 and (overall_gap or 0.0) < 0.02:
            lines.append(
                f"Observed no_call_rate is near zero ({_fmt(overall_no_call)}) and the "
                f"self-vs-topic query-overlap gap is negligible ({_fmt(overall_gap)}). "
                "The free dimensions alone do NOT evidence the thesis in this run — "
                "judged quality scoring is needed to distinguish the arms."
            )
        else:
            lines.append(
                f"Observed no_call_rate ({_fmt(overall_no_call)}) and/or the self-vs-topic "
                f"query-overlap gap ({_fmt(overall_gap)}) are non-trivial. The free dimensions "
                "show a directional signal on their own — read the per-(probe_class, "
                "corpus_scale) tables above for where it concentrates before deciding whether "
                "judged scoring is still needed to establish magnitude."
            )
    lines.append("")

    return "\n".join(lines)


def write_report(
    report: NorthStarReport,
    *,
    out_dir: Path = DEFAULT_MEASUREMENTS_DIR,
    filename: str | None = None,
) -> Path:
    """Render *report* and write it under *out_dir* (created if missing).

    Default filename is ``north-star-<YYYY-MM-DD>.md``, dated from
    ``report.generated``'s ISO-timestamp date prefix. Non-clobbering: a
    same-day rerun gets a numeric suffix (``-2``, ``-3``, ...) rather than
    overwriting an earlier report -- identical convention to
    ``src/athenaeum/shadow_parity.py``'s ``write_report`` (issue
    athenaeum#1333), so a partial run's own artifact always survives a
    same-day retry.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    if filename is None:
        filename = f"north-star-{report.generated[:10]}.md"
    name_path = Path(filename)
    stem, suffix = name_path.stem, name_path.suffix
    path = out_dir / filename
    n = 2
    while path.exists():
        path = out_dir / f"{stem}-{n}{suffix}"
        n += 1
    path.write_text(render_report(report), encoding="utf-8")
    return path
