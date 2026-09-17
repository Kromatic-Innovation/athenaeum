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
  (``RolloutRecord.turn_tokens``), not summed per task, so a push arm's
  ``injected_context_tokens`` (paid whether the delivered content was used
  or not) can be read directly beside PULL's turn cost (paid only when it
  calls) -- the asymmetry the issue names as the actual economic question.
* **Efficiency** -- turns to answer, tool-call count.
* **Waste** -- delivered pages never cited later in the rollout, both as a
  page-count fraction and as an approximate token figure.
* **Utilization** -- the FREE portion only: uid citation (reusing
  :func:`tests.evals.metrics.uids_from_recall_output`, the same extractor
  ``recall_search``'s own rendered output already round-trips through) and
  distinctive n-gram overlap (:func:`distinctive_ngram_overlap`). The
  judged per-claim support check is explicitly out of scope (issue's own
  acceptance criterion: no LLM judge anywhere in this module).
  **Issue athenaeum#1574:** the breadcrumb arms (``push_breadcrumb``,
  ``push_breadcrumb_pull``) get n-gram utilization RECOMPUTED against their
  actual (small) delivered payload (:func:`delivered_text_for_utilization`),
  never measured against the five-page basis they never received. Their
  uid-citation/waste figures render ``n/a``, not a silent near-zero: the
  shipped hook's breadcrumb bullet carries no uid marker at all, so there
  is no textual basis to compute a citation rate against
  (:func:`delivered_uids_for_utilization`'s own docstring has the detail).
* **Correctness** (issue athenaeum#1573) -- the dimension every arm above
  was missing: did the final answer actually get the ground truth right.
  Graded by :func:`grade_correctness`, a normalized substring match against
  each probe's planted ``answer_tokens`` (or a declining-language rule for
  abstention probes) -- still no LLM judge. This is what makes the NONE
  (floor) and ORACLE (ceiling) arms readable as numbers, and what a
  :func:`weak_probes` probe list is built from (probes the floor already
  answers correctly, which is a corpus-leak signal, not a retrieval win).

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
from collections import defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path

from athenaeum.push_metrics import estimate_tokens
from athenaeum.shadow_linkage import _get_git_sha, _get_version
from athenaeum.store import now_iso

# ``DISTINCTIVE_NGRAM_SIZE`` is re-exported explicitly (``as`` itself): this
# module published it before issue athenaeum#1585 moved the definition into
# ``athenaeum.text_overlap``, and the report's prose still points readers here.
from athenaeum.text_overlap import (
    DISTINCTIVE_NGRAM_SIZE as DISTINCTIVE_NGRAM_SIZE,
)
from athenaeum.text_overlap import (
    distinctive_ngram_overlap,
    ngrams,
)
from tests.evals.containment import GridCell, ResultStore
from tests.evals.corpus import Corpus, Observation, Probe, build_corpus
from tests.evals.corpus import _content_terms as _content_terms
from tests.evals.metrics import uids_from_recall_output
from tests.evals.rollout import Arm, RolloutRecord

#: The SIZE axis only (issue athenaeum#1725's crossover-scale dimension).
#: ``medium_dense``/``medium_verydense`` are the CONFUSABILITY axis (fixed
#: page count, rising near-miss density -- see ``tests.evals.corpus.Scale``'s
#: own docstring) and are deliberately NOT part of this ordering: crossover
#: asks "at what SIZE does Athenaeum start winning", and folding a
#: confusability point into the size axis would answer a different question.
SIZE_SCALE_ORDER: tuple[str, ...] = ("core", "small", "medium", "large")

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

#: ``_content_terms`` (and the stopword list / word regex behind it) moved to
#: ``tests.evals.corpus`` (issue athenaeum#1737): ``validate_core``'s
#: ``follow_through`` check needs the SAME content-term definition this
#: module's Jaccard overlap uses, and this module already imports FROM
#: ``tests.evals.corpus`` -- the reverse import would be circular. Re-imported
#: above under this module's original name so no caller here needed to change.


def lexical_overlap(a: str, b: str) -> float:
    """Jaccard overlap between the content-term sets of *a* and *b*.

    ``overlap(a, b) = |terms(a) & terms(b)| / |terms(a) | terms(b)|``
    -- ``terms()`` is lowercased alphanumeric tokens of length >= 3, minus
    :data:`tests.evals.corpus._STOPWORDS`. ``0.0`` when either side has no
    content terms (never a division by zero, and never treated as "perfect
    overlap").
    """
    ta, tb = _content_terms(a), _content_terms(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


#: Issue athenaeum#1585 moved the shingle-overlap definition into
#: ``athenaeum.text_overlap`` so ``push_metrics.determine_references`` and this
#: report compute the SAME content signal from ONE definition. Re-exported
#: under the names this module has always published, so every caller here and
#: in ``used_heuristic.py`` is unaffected.
_ngrams = ngrams


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
# Correctness grading (issue athenaeum#1573): the NONE (floor) and ORACLE
# (ceiling) arms had nothing to grade until now -- every dimension above
# describes retrieval or cost, never whether the FINAL ANSWER was actually
# right. ``Probe.answer_tokens`` (``tests/evals/corpus.py``) plants a unique,
# invented token in the body of one of a non-abstention probe's own
# ``expected_uids`` pages; grading is a plain, normalized substring match --
# no LLM judge, matching this module's own "no LLM judge anywhere" invariant.
#
# Abstention probes carry no ``answer_tokens`` (nothing in the corpus answers
# them) and are graded by a SEPARATE rule: an answer is correct abstention
# only when it asserts NONE of the corpus's planted tokens (no confabulation
# of some OTHER probe's answer) AND uses recognizably declining language
# (:data:`_NOT_FOUND_PHRASES`) -- absence of a token alone is not proof the
# arm actually declined rather than confidently asserting something else
# wrong that happens not to collide with a planted token.
# ---------------------------------------------------------------------------

#: Phrases an abstention answer is checked for, alongside the token-absence
#: check -- deliberately short and generic (this is a floor/ceiling number,
#: not a judged classification) rather than an attempt at exhaustive NLI.
_NOT_FOUND_PHRASES: tuple[str, ...] = (
    "i don't know",
    "i do not know",
    "not found",
    "no information",
    "not in the corpus",
    "cannot find",
    "can't find",
    "unable to find",
    "i'm not sure",
    "i am not sure",
    "no record of",
    "does not contain",
    "doesn't contain",
)


def _normalize_for_match(text: str) -> str:
    """Lowercased text for a normalized substring match. Deliberately
    minimal -- no stemming/punctuation-stripping -- because the tokens
    planted by the corpus are single invented words with no natural
    inflection to normalize away."""
    return text.lower()


def _all_answer_tokens(corpus: Corpus) -> frozenset[str]:
    """Every planted answer token across every probe in *corpus*. An
    abstention probe's confabulation check needs the WHOLE corpus's tokens,
    not just its own -- it has none of its own by construction."""
    return frozenset(token for probe in corpus.probes for token in probe.answer_tokens)


def grade_correctness(record: RolloutRecord, probe: Probe, corpus: Corpus) -> bool | None:
    """Did *record*'s answer get *probe*'s ground truth right?

    Non-abstention: normalized substring match -- ALL of ``probe.answer_tokens``
    must appear in the answer (mirrors the multi-hop/disambiguation probes'
    own ground truth, where surfacing only one of several required facts is a
    documented incomplete answer, not a correct one). Returns ``None`` (never
    ``False``) when the probe carries no tokens at all -- a corpus authoring
    gap that :func:`tests.evals.corpus.validate_core` already refuses to let
    ship, not a graded miss.

    Abstention: correct only when the answer asserts none of the corpus's
    planted tokens AND uses recognizable declining language -- see the
    section docstring above.
    """
    answer = _normalize_for_match(record.answer)
    if probe.probe_class == "abstention":
        if any(_normalize_for_match(tok) in answer for tok in _all_answer_tokens(corpus)):
            return False
        return any(phrase in answer for phrase in _NOT_FOUND_PHRASES)
    if not probe.answer_tokens:
        return None
    return all(_normalize_for_match(tok) in answer for tok in probe.answer_tokens)


def weak_probes(rows: Sequence[RolloutRow]) -> tuple[str, ...]:
    """Probe ids the NONE arm (no context at all) already answers correctly.

    A NONE-arm correct answer is a floor-leak signal -- the model's own prior
    knowledge (or a guessable token) already covers the ground truth -- not
    evidence that any retrieval arm helped. Sorted, deduplicated, empty when
    no such probe was observed in *rows*.

    Abstention probes are excluded by construction. Their "correct" NONE
    answer is the model declining to answer with no context at all, which is
    the expected null result, not prior knowledge leaking through the floor.
    Listing them here would put every abstention probe in the weak list on
    every run and drown the signal this list exists to carry.
    """
    ids: set[str] = set()
    for row in rows:
        if row.record.arm is not Arm.NONE:
            continue
        probe = _probe_for_row(row)
        if probe.probe_class == "abstention":
            continue
        corpus = _corpus_for_scale(row.record.corpus_scale)
        if grade_correctness(row.record, probe, corpus):
            ids.add(probe.id)
    return tuple(sorted(ids))


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
    that never called recall.

    Issue athenaeum#1574 (AC4): the breadcrumb arms' delivered text is the
    ACTUAL breadcrumb payload the shipped hook produced (or, for
    PUSH_BREADCRUMB_PULL, that payload plus whatever the recall tool
    additionally returned) -- utilization is recomputed against what was
    really delivered, never left to silently read near-zero against a
    five-page basis that was never sent.
    """
    record = row.record
    if record.arm is Arm.PUSH_PAGES_UPPER_BOUND:
        return _push_delivered_text(record)
    if record.arm is Arm.ORACLE:
        return _target_page_text(_probe_for_row(row), _corpus_for_scale(record.corpus_scale))
    if record.arm is Arm.PULL:
        return _pull_delivered_text(record)
    if record.arm is Arm.PUSH_BREADCRUMB:
        return _push_delivered_text(record)
    if record.arm is Arm.PUSH_BREADCRUMB_PULL:
        breadcrumb = _push_delivered_text(record)
        pulled = _pull_delivered_text(record)
        return "\n\n".join(part for part in (breadcrumb, pulled) if part)
    return ""


def delivered_uids_for_utilization(row: RolloutRow) -> tuple[str, ...]:
    """Uids of the pages actually delivered for *row*.

    PUSH_PAGES_UPPER_BOUND and PULL are both served by the SAME
    ``recall_search`` rendering (``**Uid:**`` marker), so
    :func:`~tests.evals.metrics.uids_from_recall_output` applies unchanged
    to either one. ORACLE's context is the ground-truth pages verbatim
    (:func:`tests.evals.rollout._oracle_context`) rendered via
    ``Page.to_markdown()`` -- plain ``uid:`` frontmatter, not the bold
    marker -- so ORACLE uses the probe's own ``expected_uids`` directly
    rather than mis-parsing a format that was never meant to match.

    PUSH_BREADCRUMB is a structural ``()``, NOT a stand-in for "delivered
    nothing" (issue athenaeum#1574 AC4): the shipped hook's breadcrumb
    bullet is ``name`` or ``name — description`` -- it carries NO uid
    marker at all (see ``examples/claude-code/user-prompt-recall.sh``'s
    render loop), so there is no textual basis to recover which pages were
    delivered. ``uid_citation_rate``/waste therefore render ``n/a`` for
    this arm, which is the CORRECT "not applicable" reading, never a
    silently-computed near-zero. PUSH_BREADCRUMB_PULL, if it actually
    called recall, DOES carry uid markers in the pulled portion (the SAME
    ``recall_search`` rendering PULL gets), so its uids come from there --
    the breadcrumb portion contributes none, for the identical reason.
    """
    record = row.record
    if record.arm is Arm.PUSH_PAGES_UPPER_BOUND:
        return tuple(uids_from_recall_output(_push_delivered_text(record)))
    if record.arm is Arm.ORACLE:
        return _probe_for_row(row).expected_uids
    if record.arm is Arm.PULL:
        return tuple(uids_from_recall_output(_pull_delivered_text(record)))
    if record.arm is Arm.PUSH_BREADCRUMB_PULL:
        return tuple(uids_from_recall_output(_pull_delivered_text(record)))
    return ()


def _native_index_coverage_value(record: RolloutRecord) -> float | None:
    """Read ``transcript[0]["native_memory"]["coverage"]`` back out of a
    NATIVE_INDEX row -- the SAME leading-dict idiom
    ``run_push_breadcrumb_pull`` already uses for its own arm metadata (see
    ``tests.evals.rollout.run_native_index``). Never raises on a malformed
    or missing shape; returns ``None`` rather than fabricating a number.
    """
    if not record.transcript:
        return None
    entry = record.transcript[0]
    if not isinstance(entry, dict):
        return None
    native = entry.get("native_memory")
    if not isinstance(native, dict):
        return None
    coverage = native.get("coverage")
    if isinstance(coverage, bool) or not isinstance(coverage, (int, float)):
        return None
    return float(coverage)


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

    # Correctness (issue athenaeum#1573) -- all arms; None when no probe in
    # the group carries ground truth (answer_tokens) to grade against.
    correctness_rate: float | None

    # Index coverage (issue athenaeum#1725, NATIVE_INDEX only) -- the
    # fraction of the corpus the TRUNCATED index still names, read back from
    # what Claude Code actually loaded. None (never 0.0) for every other arm
    # -- "no index to speak of" is a different fact from "0% coverage".
    mean_index_coverage: float | None


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
        correctness_flags: list[float] = []
        index_coverages: list[float] = []

        for row in group:
            record = row.record
            turn_counts.append(float(record.turn_count))
            tool_call_counts.append(float(len(record.tool_calls)))
            for turn in record.turn_tokens:
                input_per_turn.append(float(turn.input_tokens))
                output_per_turn.append(float(turn.output_tokens))

            probe = _probe_for_row(row)
            corpus = _corpus_for_scale(corpus_scale)

            if record.arm is Arm.PULL:
                no_call_flags.append(0.0 if record.recall_called else 1.0)
                if record.recall_called:
                    target = _target_page_text(probe, corpus)
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

            correct = grade_correctness(record, probe, corpus)
            if correct is not None:
                correctness_flags.append(1.0 if correct else 0.0)

            if record.arm is Arm.NATIVE_INDEX:
                coverage_value = _native_index_coverage_value(record)
                if coverage_value is not None:
                    index_coverages.append(coverage_value)

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
                correctness_rate=_mean(correctness_flags),
                mean_index_coverage=(
                    _mean(index_coverages) if arm == Arm.NATIVE_INDEX.value else None
                ),
            )
        )
    return stats


# ---------------------------------------------------------------------------
# Crossover scale (issue athenaeum#1725) -- the number the decision turns on
# ---------------------------------------------------------------------------

#: Athenaeum arms counted toward "Athenaeum's correctness" in the crossover
#: definition -- the DELIVERY arms only. ``none`` (the floor) and ``oracle``
#: (the ceiling) are excluded deliberately: neither is a shippable
#: configuration, so neither should be able to make Athenaeum look like it
#: has "crossed over" native memory.
_ATHENAEUM_DELIVERY_ARM_VALUES = frozenset(
    {
        Arm.PUSH_PAGES_UPPER_BOUND.value,
        Arm.PUSH_BREADCRUMB.value,
        Arm.PUSH_BREADCRUMB_PULL.value,
        Arm.PULL.value,
    }
)

#: The two native arms -- "native's correctness" is the best of these two.
_NATIVE_ARM_VALUES = frozenset({Arm.NATIVE_INDEX.value, Arm.NATIVE_GREP.value})


def crossover_scales(stats: Sequence[GroupStats]) -> dict[str, str]:
    """The smallest scale, per probe class, at which Athenaeum's correctness
    exceeds native's (design doc §6/§7: "the number the decision turns on").

    **Definition:** "Athenaeum's correctness" = the best ``correctness_rate``
    among the Athenaeum DELIVERY arms (:data:`_ATHENAEUM_DELIVERY_ARM_VALUES`)
    -- explicitly excluding ``none`` (the floor) and ``oracle`` (the ceiling),
    because neither is a shippable configuration. "Native's correctness" =
    the best ``correctness_rate`` among ``native_index``/``native_grep``
    (:data:`_NATIVE_ARM_VALUES`). Only the SIZE axis
    (:data:`SIZE_SCALE_ORDER`) is walked, smallest first; the
    confusability scales (``medium_dense``/``medium_verydense``) are a
    different axis entirely and never considered here.

    A probe class absent from the returned mapping had insufficient data
    (no correctness figure on one side, at every size scale) to establish a
    crossover -- render ``"n/a"`` for it, never a fabricated scale. Pure
    function: no I/O, no corpus rebuild, operates only on already-computed
    :class:`GroupStats`.
    """
    by_probe_class: dict[str, dict[str, list[GroupStats]]] = defaultdict(lambda: defaultdict(list))
    for stat in stats:
        by_probe_class[stat.probe_class][stat.corpus_scale].append(stat)

    result: dict[str, str] = {}
    for probe_class, by_scale in by_probe_class.items():
        for scale in SIZE_SCALE_ORDER:
            group = by_scale.get(scale)
            if not group:
                continue
            athenaeum_rates = [
                s.correctness_rate
                for s in group
                if s.arm in _ATHENAEUM_DELIVERY_ARM_VALUES and s.correctness_rate is not None
            ]
            native_rates = [
                s.correctness_rate
                for s in group
                if s.arm in _NATIVE_ARM_VALUES and s.correctness_rate is not None
            ]
            if not athenaeum_rates or not native_rates:
                continue
            if max(athenaeum_rates) > max(native_rates):
                result[probe_class] = scale
                break
    return result


# ---------------------------------------------------------------------------
# Write-path stats (issue athenaeum#1726 AC4, design lock
# docs/design/native-memory-baseline.md §5 "Phase 2")
#
# Everything above grades a READ over an already-finished store. This
# section grades the WRITE that produced one: given the SAME
# ``tests.evals.corpus.Observation`` stream, whose resulting store (an
# Athenaeum-compiled wiki, or a native auto-memory directory) still carries
# the planted ``answer_tokens`` -- the first measurement of the observation
# filter against a baseline other than itself (design doc §5).
#
# Deliberately NOT folded into ``GroupStats``/``compute_group_stats``: those
# are keyed by (probe_class, corpus_scale, arm) because every dimension they
# hold is a property of ANSWERING a probe. A write-path measurement has no
# probe or arm at all -- it is a property of the COMPILE run itself, over
# the whole observation stream at once -- so forcing it through the same
# per-probe-class grouping would either fabricate a class it does not have
# or silently pick one arbitrarily. It is reported per (system, corpus_scale)
# instead, its own natural grain, and rendered as its own report section.
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class WritePathStats:
    """Phase 2 write-path measurement for ONE system at one corpus scale.

    Every count below is scoped to the observations that carry at least one
    planted ``answer_tokens`` value -- an observation with no token plants
    nothing this scanner can check, so it is excluded from the denominator
    rather than silently counted as "retained". ``None`` (never a fabricated
    ``0``) when *observations* carries no token-bearing entry at all --
    "nothing was measurable" is a different fact from "everything was lost",
    the same discipline :class:`GroupStats` already holds every other
    optional field to.
    """

    system: str
    corpus_scale: str
    pages_targeted: int
    pages_written: int | None
    answer_tokens_total: int
    answer_tokens_retained: int | None
    observations_total: int
    observations_measured: int
    observations_dropped: int | None


def compute_write_path_stats(
    system: str,
    corpus_scale: str,
    observations: Sequence[Observation],
    store_files: Mapping[str, str],
) -> WritePathStats:
    """Score one system's compiled store against the observation stream
    that produced it.

    *store_files* is whatever ended up in the system's store, as plain
    ``{path: text}`` -- an Athenaeum wiki tree (one entry per compiled
    page) or a native auto-memory directory
    (:attr:`~tests.evals.rollout.NativeWriterResult.memory_files`). Both are
    directories of markdown text, so a single substring scan over the
    concatenation serves either one identically; this function never
    inspects frontmatter or file naming, precisely because a native store's
    filenames are the MODEL's own choice and carry no correspondence to
    ``page_uid`` a grader could rely on.

    A page counts as **written** if at least one of ITS OWN planted tokens
    (from any of its token-bearing observations) is found anywhere in
    *store_files* -- content-addressed, not by filename, for the same
    reason. An observation counts as **dropped** if it carried at least one
    token and not every one of its tokens survived.
    """
    corpus_text = "\n".join(store_files.values())
    token_bearing = [obs for obs in observations if obs.answer_tokens]
    all_tokens = sorted({token for obs in token_bearing for token in obs.answer_tokens})
    retained_tokens = {token for token in all_tokens if token in corpus_text}

    targeted_pages = sorted({obs.page_uid for obs in token_bearing})
    written_pages = {
        obs.page_uid
        for obs in token_bearing
        if any(token in corpus_text for token in obs.answer_tokens)
    }
    dropped = [
        obs for obs in token_bearing if not all(token in corpus_text for token in obs.answer_tokens)
    ]

    has_measurable_data = bool(token_bearing)
    return WritePathStats(
        system=system,
        corpus_scale=corpus_scale,
        pages_targeted=len(targeted_pages),
        pages_written=len(written_pages) if has_measurable_data else None,
        answer_tokens_total=len(all_tokens),
        answer_tokens_retained=len(retained_tokens) if all_tokens else None,
        observations_total=len(observations),
        observations_measured=len(token_bearing),
        observations_dropped=len(dropped) if has_measurable_data else None,
    )


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
    # issue athenaeum#1573: probe ids the NONE arm already answers correctly.
    weak_probes: tuple[str, ...]
    # issue athenaeum#1726: Phase 2 write-path measurement, empty when this
    # run carried none (every pre-athenaeum#1726 caller of build_report).
    write_path_stats: tuple[WritePathStats, ...] = ()


def build_report(
    rows: Sequence[RolloutRow],
    *,
    aborted: bool = False,
    abort_reason: str = "",
    write_path_stats: Sequence[WritePathStats] = (),
) -> NorthStarReport:
    """Assemble a :class:`NorthStarReport` from decoded result-store rows.

    Pure computation -- constructs no model client and makes no network
    call (see the module docstring's "No LLM judge" note). *write_path_stats*
    is an independent, pre-computed input (issue athenaeum#1726): unlike
    *rows*, it cannot be derived from a :class:`ResultStore` here, since it
    is scored against an :class:`~tests.evals.corpus.Observation` stream and
    a system's raw store contents, neither of which a ``RolloutRow`` carries.
    """
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
        weak_probes=weak_probes(rows),
        write_path_stats=tuple(write_path_stats),
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

    lines.append("## Arms in this report (athenaeum#1574)")
    lines.append("")
    lines.append(
        "**PUSH means breadcrumbs** — `push_breadcrumb` and `push_breadcrumb_pull` are the "
        "arms that match what `examples/claude-code/user-prompt-recall.sh` actually ships: at "
        "most three 200-character-clamped `name — description` bullets, assembled by running "
        "that hook itself, never reimplemented. `push_pages_upper_bound` is the ORIGINAL "
        "five-full-page PUSH arm, kept and renamed — read it as an explicit **upper bound** "
        "(\"what if the model always got the whole page\"), never as the shipped configuration."
    )
    lines.append("")
    lines.append("| arm | delivery | reads as |")
    lines.append("| --- | --- | --- |")
    lines.append("| `none` | nothing | floor |")
    lines.append(
        "| `push_pages_upper_bound` | 5 full pages via `recall_search` | **upper bound**, "
        "NOT the shipped hook |"
    )
    lines.append(
        "| `push_breadcrumb` | <=3 breadcrumbs, via the real shipped hook | **matches "
        "production PUSH** |"
    )
    lines.append(
        "| `push_breadcrumb_pull` | breadcrumbs injected + `recall` tool available | matches "
        "production PUSH, agent may still PULL |"
    )
    lines.append("| `oracle` | ground-truth pages verbatim | ceiling |")
    lines.append("| `pull` | nothing injected, `recall` tool available | agent-initiated only |")
    lines.append(
        "| `native_index` | Claude Code auto-memory: topic files + a full `MEMORY.md` index, "
        "loaded and truncated by Claude Code itself | native memory, WITH an index (honest "
        "past the documented cap) |"
    )
    lines.append(
        "| `native_grep` | Claude Code auto-memory: topic files, no index at all | native "
        "memory, file search only |"
    )
    lines.append("")
    lines.append(
        "**Issue athenaeum#1725:** `native_index`/`native_grep` read a Claude Code auto-memory "
        "store instead of an Athenaeum-compiled corpus (design lock: "
        "`docs/design/native-memory-baseline.md`) -- the read-path comparison the project's "
        "continuation decision turns on. Neither is scored against a `pushed_context`/pulled-"
        "recall basis the way the other arms are: there is no Athenaeum retrieval step to "
        "attribute utilization to."
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

    lines.append("## Correctness (answer ground truth, issue athenaeum#1573)")
    lines.append("")
    lines.append(
        "`correctness_rate` grades the ANSWER, not retrieval: normalized substring match "
        "against each probe's planted `answer_tokens` (non-abstention), or the declining-"
        "language rule for abstention probes — see `grade_correctness`. No LLM judge. This is "
        "what makes NONE (floor) and ORACLE (ceiling) readable as numbers for the first time — "
        "every other dimension above describes retrieval or cost, never whether the final "
        "answer was actually right. `n/a` means no probe in that group carries ground truth "
        "tokens to grade against."
    )
    lines.append("")
    lines.append("| probe_class | corpus_scale | arm | n | correctness_rate |")
    lines.append("| --- | --- | --- | --- | --- |")
    for s in report.stats:
        lines.append(
            f"| {s.probe_class} | {s.corpus_scale} | {s.arm} | {s.n} | "
            f"{_fmt(s.correctness_rate)} |"
        )
    lines.append("")

    lines.append("### Weak probes (NONE arm already answers correctly)")
    lines.append("")
    lines.append(
        "Probes where the floor (no context at all) already grades correct — a signal the "
        "ground truth leaked into the model's own prior knowledge, or the token is otherwise "
        "guessable, not that any retrieval arm helped. Read alongside the ORACLE ceiling: a "
        "probe listed here needs a harder token or a different question, not a better arm."
    )
    lines.append("")
    if report.weak_probes:
        for probe_id in report.weak_probes:
            lines.append(f"- {probe_id}")
    else:
        lines.append("_none observed in this run_")
    lines.append("")

    lines.append("## Index coverage (NATIVE_INDEX only, athenaeum#1725)")
    lines.append("")
    lines.append(
        "`index_coverage = index_lines_loaded / pages` -- the fraction of the corpus the "
        "TRUNCATED index still names. `index_lines_loaded` is read back from what Claude "
        "Code actually loaded into the session (the `attachment` event in the session "
        "transcript), never recomputed from the 200-line/25KB cap: Claude Code performs the "
        "truncation, this report only observes its result. `n/a` means no NATIVE_INDEX row "
        "in that group carried a readable coverage figure (e.g. auto memory did not activate)."
    )
    lines.append("")
    lines.append("| probe_class | corpus_scale | n | index_coverage |")
    lines.append("| --- | --- | --- | --- |")
    for s in report.stats:
        if s.arm != Arm.NATIVE_INDEX.value:
            continue
        lines.append(
            f"| {s.probe_class} | {s.corpus_scale} | {s.n} | {_fmt(s.mean_index_coverage)} |"
        )
    lines.append("")

    lines.append("## Write path (Phase 2, athenaeum#1726)")
    lines.append("")
    lines.append(
        "Given the SAME observation stream, whose resulting store still carries the planted "
        "`answer_tokens` -- the write half of the comparison (design doc "
        "`docs/design/native-memory-baseline.md` §5), and the first measurement of the "
        "observation filter against a baseline other than itself. Scoped to observations "
        "carrying at least one planted token (`observations_measured` of `observations_total`); "
        "an observation with no token plants nothing this scanner can check. `n/a` means no "
        "token-bearing observation was present for that system/scale, never a fabricated 0."
    )
    lines.append("")
    if report.write_path_stats:
        lines.append(
            "| system | corpus_scale | pages_targeted | pages_written | answer_tokens_total | "
            "answer_tokens_retained | observations_total | observations_measured | "
            "observations_dropped |"
        )
        lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
        for w in report.write_path_stats:
            lines.append(
                f"| {w.system} | {w.corpus_scale} | {w.pages_targeted} | "
                f"{w.pages_written if w.pages_written is not None else 'n/a'} | "
                f"{w.answer_tokens_total} | "
                f"{w.answer_tokens_retained if w.answer_tokens_retained is not None else 'n/a'} | "
                f"{w.observations_total} | {w.observations_measured} | "
                f"{w.observations_dropped if w.observations_dropped is not None else 'n/a'} |"
            )
    else:
        lines.append("_no Phase 2 write-path data in this run_")
    lines.append("")

    lines.append("## Crossover scale (athenaeum#1725)")
    lines.append("")
    lines.append(
        "The smallest scale, per probe class, at which Athenaeum's correctness exceeds "
        "native's -- \"the number the decision turns on\" (design doc "
        "`docs/design/native-memory-baseline.md` §6/§7). Walks the SIZE axis only "
        "(`core` < `small` < `medium` < `large`); the `medium_dense`/`medium_verydense` "
        "confusability scales are a different axis and are never considered here. "
        "**Athenaeum's correctness** = the best `correctness_rate` among its DELIVERY arms "
        "only (`push_pages_upper_bound`, `push_breadcrumb`, `push_breadcrumb_pull`, `pull`) "
        "-- `none` (the floor) and `oracle` (the ceiling) are explicitly excluded, because "
        "neither is a shippable configuration. **Native's correctness** = the best "
        "`correctness_rate` among `native_index`/`native_grep`. `n/a` means insufficient "
        "data at every scale for that probe class to establish a crossover -- never a "
        "fabricated scale."
    )
    lines.append("")
    lines.append("| probe_class | crossover_scale |")
    lines.append("| --- | --- |")
    crossovers = crossover_scales(list(report.stats))
    for probe_class in sorted({s.probe_class for s in report.stats}):
        lines.append(f"| {probe_class} | {crossovers.get(probe_class, 'n/a')} |")
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
