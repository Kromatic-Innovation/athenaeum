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
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timedelta
from pathlib import Path

from athenaeum.footnote_markers import (
    INLINE_MARKER_RE,
    parse_footnote_definitions,
)
from athenaeum.models import parse_bucket, parse_frontmatter, validity_bound_str
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
from tests.evals.corpus import (
    Corpus,
    Observation,
    Probe,
    answer_bearing_uids,
    build_corpus,
    deep_hop_uids,
)
from tests.evals.corpus import _content_terms as _content_terms
from tests.evals.corpus import _normalize_marker_for_match as _normalize_marker_for_match
from tests.evals.metrics import uids_from_recall_output
from tests.evals.rollout import Arm, RolloutRecord

#: The SIZE axis only (issue athenaeum#1725's crossover-scale dimension).
#: ``medium_dense``/``medium_verydense`` are the CONFUSABILITY axis (fixed
#: page count, rising near-miss density -- see ``tests.evals.corpus.Scale``'s
#: own docstring) and are deliberately NOT part of this ordering: crossover
#: asks "at what SIZE does Athenaeum start winning", and folding a
#: confusability point into the size axis would answer a different question.
SIZE_SCALE_ORDER: tuple[str, ...] = ("core", "small", "medium", "large", "xlarge")

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
    file yet (a fresh, never-run store).

    Torn and duplicate rows are handled rather than fatal (see
    :func:`load_rollout_rows_and_diagnostics`). Use that function when the
    counts matter -- a report should say how many rows it could not read,
    not merely not crash.
    """
    # ``list``, not the diagnostics' tuple: this function's contract is a
    # list and callers compare against one.
    return list(load_rollout_rows_and_diagnostics(store).rows)


@dataclasses.dataclass(frozen=True)
class StoreDiagnostics:
    """One read of a result store: its rows, and what was wrong with it."""

    #: One row per DISTINCT cell key, in first-seen order.
    rows: tuple[RolloutRow, ...]
    #: Lines that would not decode as a rollout row.
    torn: int
    #: Rows superseded by a later row for the same cell key.
    duplicates: int


def load_rollout_rows_and_diagnostics(store: ResultStore) -> StoreDiagnostics:
    """Read *store* once, de-duplicated by cell key, last row winning.

    **Why the reader de-duplicates at all.** ``ResultStore`` is append-only
    and never de-duplicates on write, and this driver's resume granularity
    is the whole (probe, corpus_scale, replicate) GROUP: a group that
    stopped part-way is re-run in full, and every one of its rows is
    appended fresh alongside the ones that had already landed. So duplicate
    cell keys are a NORMAL consequence of resuming, not a corruption. Left
    un-deduped they would inflate every count computed from ``rows`` --
    including the partial banner's numerator, which could then exceed the
    planned total and hide a partial run behind an apparently-complete one.

    **Last wins**, because the later row is the one the re-run produced:
    the earlier one belongs to an attempt that did not finish, and the
    store's own ordering is append order.

    A row that decodes as JSON but is missing a field this decoder needs
    counts as torn too -- a partial write can, rarely, leave something that
    parses but is not a row.
    """
    if not store.path.exists():
        return StoreDiagnostics(rows=(), torn=0, duplicates=0)
    by_key: dict[str, RolloutRow] = {}
    torn = 0
    duplicates = 0
    with store.path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
                cell = GridCell(
                    probe=raw["probe_id"],
                    arm=raw["arm"],
                    corpus_scale=raw["corpus_scale"],
                    replicate=int(raw["replicate"]),
                )
                record = RolloutRecord.from_payload(raw)
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                torn += 1
                continue
            key = cell.cell_key()
            if key in by_key:
                duplicates += 1
            by_key[key] = RolloutRow(cell=cell, record=record)
    return StoreDiagnostics(rows=tuple(by_key.values()), torn=torn, duplicates=duplicates)


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
# them) and are graded by a SEPARATE rule, :func:`_is_abstention`: an answer
# is correct abstention only when (a) it asserts NONE of the corpus's
# planted tokens AND NONE of the corpus's planted answer_markers -- no
# confabulation of some OTHER probe's answer, by tag or by content -- AND
# (b) it uses recognizably declining language (:data:`_NOT_FOUND_PHRASES`
# plus the absence-phrase families below) -- absence of a token alone is not
# proof the arm actually declined rather than confidently asserting
# something else wrong that happens not to collide with a planted token.
#
# athenaeum#1836: a corpus distractor plants a REAL adjacent-topic page (the
# medium scale's PTO page sits next to the absent parental-leave policy), so
# a correct abstention often reads "there is no formal parental leave policy
# documented" -- a phrasing the original flat ``_NOT_FOUND_PHRASES`` list
# never covered -- rather than "not found"/"no record of". The same run
# also showed that an honest abstention routinely CITES the adjacent true
# fact it did find ("what is documented is the PTO policy: 25 days per
# year", or the PTO page's reference tag) while declining the asked
# question -- so neither a planted-token nor a planted-marker deny-list is
# applied to abstention answers any more: under the operator ruling on
# athenaeum#1791 ("if the answer is right, it is right") a decline that
# also states true neighbouring facts is a correct abstention. A
# confidently WRONG value for the asked question is not detectable by
# planted markers (an absent policy plants nothing), so that residual risk
# is documented here rather than papered over with a guard that fails the
# honest case; `grade_harm` (forbidden tokens) remains the wrong-answer
# check where a probe plants one.
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
    # Sixth grid (2026-09-18, run 35305241221): 16 of 18 oracle abstention
    # rows declined with the shapes below and none of the phrases above,
    # grading a correct refusal as wrong (athenaeum#1788 gate). Each is a
    # statement of missing information, never a hedge that could dress up a
    # confabulated answer -- the planted-token deny-list above still runs
    # first.
    "don't have access",
    "do not have access",
    "don't have any information",
    "do not have any information",
    "don't have information",
    "do not have information",
    "don't have any context",
    "do not have any context",
    "no context provided",
    "not in the knowledge base",
    "not included in the",
    "couldn't find",
    "could not find",
    "found no ",
    "don't see",
    "do not see",
)

#: Absence-phrase families (issue athenaeum#1836): a correct abstention
#: often names the missing thing ("no formal policy", "policy ...
#: documented") rather than declining outright. Substrings, deliberately --
#: this is a floor/ceiling grader, not NLI; see the block comment above
#: for why no deny-list accompanies it.
_ABSENCE_PHRASE_FAMILIES: tuple[str, ...] = (
    "no documented",
    "no formal",
    "no formalised",
    "no formalized",
    "no established",
    "not documented",
    "not formalised",
    "not formalized",
    "not established",
    "not have a document",
    "not have a formal",
    "not have an established",
    "nothing about",
    "not mentioned",
    "isn't mentioned",
    "is not mentioned",
    "could not locate",
    "couldn't locate",
    "cannot locate",
    "can't locate",
)

#: All abstention-declining phrases: the original flat list plus the
#: absence-phrase families above.
_ABSTENTION_PHRASES: tuple[str, ...] = _NOT_FOUND_PHRASES + _ABSENCE_PHRASE_FAMILIES


def _normalize_for_match(text: str) -> str:
    """Lowercased text for a normalized substring match. Deliberately
    minimal -- no stemming/punctuation-stripping -- because the tokens
    planted by the corpus are single invented words with no natural
    inflection to normalize away.

    DO NOT widen this (issue athenaeum#1843). It is read by
    :func:`_is_abstention`, :func:`tag_followed`, :func:`grade_harm`,
    :func:`grade_coverage` and :func:`grade_marker_resolution` as well as by
    :func:`grade_correctness`'s answer_tokens path; a looser match here can
    flip a correct abstention to wrong. ``answer_markers`` comparisons use
    :func:`tests.evals.corpus._normalize_marker_for_match` instead, which
    additionally collapses whitespace and is scoped to exactly those
    comparisons.
    """
    return text.lower()


def _all_answer_tokens(corpus: Corpus) -> frozenset[str]:
    """Every planted answer token across every probe in *corpus*. An
    abstention probe's confabulation check needs the WHOLE corpus's tokens,
    not just its own -- it has none of its own by construction."""
    return frozenset(token for probe in corpus.probes for token in probe.answer_tokens)


def _is_abstention(answer: str, probe: Probe, corpus: Corpus) -> bool:
    """Does *answer* read as a correct abstention for *probe* (issue
    athenaeum#1836)?

    True when the answer contains declining language: one of
    :data:`_ABSTENTION_PHRASES` (the original flat phrase list plus the
    absence-phrase families for "no documented/formal/established X",
    "nothing about X", "could not find/locate", "not mentioned").

    Deliberately NO planted-token or planted-marker deny-list (operator
    ruling on athenaeum#1791, applied on athenaeum#1736 from run
    35399179014): every arm's honest abstention on the medium corpus's
    parental-leave probe cited the adjacent PTO fact it did find ("25 days
    per year", or that page's reference tag) while declining, and a
    deny-list graded all of them wrong. Mentioning a true neighbouring fact
    is not answering the asked question.

    *probe* and *corpus* are unused today (abstention probes plant no
    tokens or markers of their own) but are kept in the signature so a
    future per-probe exception does not require touching every call site.
    """
    del probe, corpus
    normalized = _normalize_for_match(answer)
    return any(phrase in normalized for phrase in _ABSTENTION_PHRASES)


def _native_loaded_uids(record: RolloutRecord) -> tuple[str, ...]:
    """Uids of memory files the NATIVE_GREP arm actually opened via its own
    file tools (issue athenaeum#1793).

    ``transcript[0]["native_memory"]["loaded_memory_files"]`` (see
    :func:`tests.evals.rollout.run_native_grep`) is keyed by the path of
    every memory file the arm's read tool actually returned content for --
    ``materialize_native_memory`` names each topic file ``<uid>.md``,
    matching :attr:`tests.evals.corpus.Page.filename` -- so the path stem
    IS the delivered page's uid, the same tool-output basis PULL's uid
    extraction reads from :func:`uids_from_recall_output`.

    Also used for NATIVE_INDEX as of issue athenaeum#1831:
    ``run_native_index`` now records the SAME ``loaded_memory_files`` shape
    -- every file Claude Code's own ``read``/``grep`` tools actually opened
    during the turn, from the SAME ``_spawn_native`` return value
    ``run_native_grep`` already reads (see ``run_native_index``'s own
    comment: it used to discard everything but the ``MEMORY.md`` text).
    Deliberately NOT the loaded ``MEMORY.md`` INDEX text itself, even though
    that text is also available: at ``core`` scale the untruncated index
    names every page in the corpus, so "named in the index" would be true
    unconditionally -- a tautology, not evidence of anything the model did.
    A topic file actually opened via ``read`` is real per-page evidence.
    """
    if not record.transcript:
        return ()
    entry = record.transcript[0]
    if not isinstance(entry, dict):
        return ()
    native = entry.get("native_memory")
    if not isinstance(native, dict):
        return ()
    loaded = native.get("loaded_memory_files")
    if not isinstance(loaded, dict):
        return ()
    return tuple(Path(path_str).stem for path_str in loaded if Path(path_str).stem)


def _breadcrumb_delivered_uids(
    record: RolloutRecord, probe: Probe, corpus: Corpus
) -> tuple[str, ...]:
    """Uids of *probe*'s ``expected_uids`` pages actually NAMED in the
    PUSH_BREADCRUMB arm's delivered text (issue athenaeum#1831).

    The shipped hook (``examples/claude-code/user-prompt-recall.sh``) emits
    at most three lines shaped ``  - <name> -- <description>`` (BM25-ranked,
    ``LIMIT 3``) -- no uid marker at all, so :func:`uids_from_recall_output`
    cannot be reused here. But the bullet's own selection IS a real,
    SELECTIVE retrieval signal (top-3 of the whole corpus) -- naming a page
    is the entirety of what this arm delivers about it, so "named in the
    breadcrumb" is the correct delivered-evidence reading for this arm, a
    weaker claim than the recall arms' full-content delivery. Matched on the
    bullet's own name field (split on the em dash), not a loose substring
    scan, so a page whose name happens to appear inside another page's
    description does not false-positive.
    """
    text = _push_delivered_text(record)
    if not text:
        return ()
    bullet_names: set[str] = set()
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("-"):
            continue
        body = stripped.lstrip("-").strip()
        name_part = body.split("—")[0].strip()
        if name_part:
            bullet_names.add(name_part)
    if not bullet_names:
        return ()
    pages_by_uid = {page.uid: page for page in corpus.pages}
    return tuple(
        uid
        for uid in probe.expected_uids
        if uid in pages_by_uid and pages_by_uid[uid].name in bullet_names
    )


def _delivered_uids(record: RolloutRecord, probe: Probe, corpus: Corpus) -> tuple[str, ...]:
    """Uids of the pages actually delivered to *record*'s arm for this cell.

    The GRADER's delivered-page-evidence rule (issue athenaeum#1831,
    superseding athenaeum#1793's uid-citation-in-answer-text rule), keyed
    off the bare ``record``/``probe``/``corpus`` triple every existing
    caller and test already has in hand -- no ``RolloutRow`` required.

    **Not** the dispatch behind :func:`delivered_uids_for_utilization`
    (issue athenaeum#1842): that function carries its OWN arm dispatch, and
    a prior version of this docstring claiming the two shared one was false
    -- the two answer different questions and have deliberately diverged.
    ``delivered_uids_for_utilization`` measures WASTE, so it counts only
    pages a caller-assembled payload or a ``recall_search`` rendering PUT in
    front of the model whether it wanted them or not; it covers four arms
    and structurally returns ``()`` for the rest. This function measures
    delivery EVIDENCE for correctness, so it also counts a page the model
    reached by its own tool call. Changing one does not change the other,
    and must not be assumed to.

    Per-arm: PUSH_PAGES_UPPER_BOUND and PULL both read the ``**Uid:**``
    marker out of the ``recall_search`` rendering they were served; ORACLE
    is the ground-truth pages verbatim, so its ``expected_uids`` ARE what
    was delivered. PUSH_BREADCRUMB and NATIVE_GREP/NATIVE_INDEX have real
    per-arm evidence of their own (issue athenaeum#1831 AC2) -- see
    :func:`_breadcrumb_delivered_uids` and :func:`_native_loaded_uids`.
    The two arms served the athenaeum MCP tools (PULL and
    PUSH_BREADCRUMB_PULL -- see ``tests.evals.rollout``'s
    ``PULL_ALLOWED_TOOLS`` and the api-mode ``tools=`` lists, the only arms
    that can call a tool at all) ALSO count pages fetched via
    ``read_entity`` (issue athenaeum#1842, see
    :func:`_read_entity_delivered_uids`) -- the deep hop ``recall``'s uid
    marker exists to enable, previously invisible here because that tool
    returns JSON rather than the recall markdown. Only :attr:`Arm.NONE`
    falls through to ``()`` -- it delivers no context by design.
    """
    if record.arm is Arm.PUSH_PAGES_UPPER_BOUND:
        return tuple(uids_from_recall_output(_push_delivered_text(record)))
    if record.arm is Arm.ORACLE:
        return probe.expected_uids
    if record.arm is Arm.PULL:
        pulled = tuple(uids_from_recall_output(_pull_delivered_text(record)))
        read = _read_entity_delivered_uids(record)
        return tuple(dict.fromkeys((*pulled, *read)))
    if record.arm is Arm.PUSH_BREADCRUMB:
        return _breadcrumb_delivered_uids(record, probe, corpus)
    if record.arm is Arm.PUSH_BREADCRUMB_PULL:
        breadcrumb = _breadcrumb_delivered_uids(record, probe, corpus)
        pulled = tuple(uids_from_recall_output(_pull_delivered_text(record)))
        read = _read_entity_delivered_uids(record)
        return tuple(dict.fromkeys((*breadcrumb, *pulled, *read)))
    if record.arm is Arm.NATIVE_GREP:
        return _native_loaded_uids(record)
    if record.arm is Arm.NATIVE_INDEX:
        return _native_loaded_uids(record)
    return ()


#: Identifies the GRADING RULE this report was produced under, stamped into
#: the report header beside ``corpus_digest`` (issue athenaeum#1842).
#:
#: ``Corpus.fingerprint()`` digests pages only -- it cannot move when the
#: grader changes -- so without this stamp two report tables computed over
#: the SAME stored rows under DIFFERENT grading rules are indistinguishable
#: from each other, and a reader comparing them would read a rule change as
#: a model regression (or improvement). Bump this string in the SAME commit
#: as any change to :func:`grade_correctness`, :func:`_delivered_uids` or
#: anything they dispatch to; the value is the issue that last changed the
#: rule, which is the most useful thing a reader can be handed.
GRADER_REVISION = "athenaeum#1843"


def tag_followed(record: RolloutRecord, probe: Probe) -> bool | None:
    """Report-only diagnostic (issue athenaeum#1831, operator ruling on
    athenaeum#1791 comment 5732689494: "the tag requirement is dubious as a
    correctness criterion ... the tag becomes a report-only diagnostic").

    ``True`` when the answer literally quotes every one of
    ``probe.answer_tokens``'s planted tags (the ``[ref: TAG]`` value
    ``REFERENCE_TAG_INSTRUCTION`` asks for) -- this is the OLD
    athenaeum#1753 correctness rule's tag path, demoted: it no longer feeds
    :func:`grade_correctness`, any design-doc section 7 condition, or any
    :class:`GroupStats` win/loss field, only its own report-only
    ``tag_followed_rate`` column, rendered per arm and per mode.

    ``None`` (never ``False``) for an abstention probe -- it plants no tag
    to quote -- and for a probe with no ``answer_tokens`` at all (a corpus
    authoring gap :func:`tests.evals.corpus.validate_core` already refuses
    to ship).
    """
    if probe.probe_class == "abstention" or not probe.answer_tokens:
        return None
    answer = _normalize_for_match(record.answer)
    return all(_normalize_for_match(tok) in answer for tok in probe.answer_tokens)


def marker_miss_with_delivery(
    record: RolloutRecord, probe: Probe, corpus: Corpus
) -> bool | None:
    """Report-only diagnostic (issue athenaeum#1842): was this cell graded
    ``False`` DESPITE every answer-bearing page having been delivered?

    ``True`` when both of :func:`grade_correctness`'s two conjuncts split
    apart -- every one of ``answer_bearing_uids``'s pages IS in
    :func:`_delivered_uids` for this arm/record (clause (b) satisfied), and
    at least one of those pages contributed none of its planted
    ``answer_markers`` values -- alternatives included -- to the answer under
    the same scoped marker normalizer clause (a) uses (clause (a)
    violated). The
    model was handed the page and still did not say the fact.

    This is the column that makes the NEXT grader gap self-evident. When the
    delivery channel is the thing that is broken, a cell grades ``False``
    with this ``False`` too (nothing was delivered, so there is no
    marker-miss to report); when delivery is sound and the count here is
    what is left over, the residue is either a genuine model miss or a
    marker-matching gap (whitespace, alternative phrasing) -- a distinction
    a bare ``correctness_rate`` cannot express. ``False`` (not ``None``) for
    a cell that graded correct: "delivered and the markers matched" is a
    real observation of this diagnostic, not an absence of one.

    ``report_only``, exactly as :func:`tag_followed` is: it feeds no
    design-doc section 7 condition, no :func:`compute_verdicts` input and no
    :class:`GroupStats` win/loss field -- only its own
    ``marker_miss_with_delivery`` count column.

    Returns ``None`` (never ``False``) for an abstention probe -- it plants
    no marker to miss, and :func:`grade_correctness` grades it by declining
    language instead, so there is no marker/delivery split to report; the
    same exemption :func:`tag_followed` takes, for the same reason. Over
    every NON-abstention probe the ``None`` population is exactly
    :func:`grade_correctness`'s: a probe with no ``answer_tokens``, no
    answer-bearing page, or an answer-bearing page with no
    ``answer_markers`` entry -- so on the rows where both columns report,
    "gradable cell" means the same thing in both.
    """
    if probe.probe_class == "abstention" or not probe.answer_tokens:
        return None
    pages_by_uid = {page.uid: page for page in corpus.pages}
    required_uids = answer_bearing_uids(probe, pages_by_uid)
    if not required_uids:
        return None
    markers_by_uid: dict[str, list[str]] = {}
    for uid, marker in probe.answer_markers:
        markers_by_uid.setdefault(uid, []).append(marker)
    if any(not markers_by_uid.get(uid) for uid in required_uids):
        return None
    delivered = frozenset(_delivered_uids(record, probe, corpus))
    if any(uid not in delivered for uid in required_uids):
        return False
    # The SAME comparison `grade_correctness`'s clause (a) makes, with the
    # same scoped normalizer (issue athenaeum#1843) -- this column exists to
    # split that conjunct apart, so a normalizer mismatch here would make it
    # report marker misses on cells the grader scores correct.
    marker_answer = _normalize_marker_for_match(record.answer)
    return any(
        not any(
            _normalize_marker_for_match(marker) in marker_answer
            for marker in markers_by_uid[uid]
        )
        for uid in required_uids
    )


def deep_hop_delivered(
    record: RolloutRecord, probe: Probe, corpus: Corpus
) -> bool | None:
    """Report-only diagnostic (issue athenaeum#1844): did every page that
    can ONLY be reached by following the follow_through hop actually get
    delivered to this arm?

    The deep page(s) are named by
    :func:`tests.evals.corpus.deep_hop_uids` -- the SAME predicate
    :func:`tests.evals.corpus.validate_core` shipped the probe against, so
    "the deep page" means one thing in the corpus validator and one thing in
    this report. Delivery is :func:`_delivered_uids`, the grader's own
    dispatch, so this column and ``correctness_rate`` read the same
    evidence.

    This is what separates the two failure modes a bare ``correctness_rate``
    conflates on a follow_through cell: the arm never reached the second
    page (``False`` here), versus it reached the page and the answer still
    did not carry the fact (``True`` here, with the cell graded ``False``).
    The first says fix instrumentation or the delivery channel; the second
    says fix the corpus or read it as a genuine model miss.

    Returns ``None`` for every non-``follow_through`` probe -- there is no
    hop to have followed, which is a different fact from not having followed
    one, so those groups render ``n/a`` rather than a counted zero. Also
    ``None`` for a follow_through probe with no derivable deep hop, which is
    exactly the corpus error ``validate_core`` refuses to ship: absent
    ground truth, not a negative observation.

    ``report_only``, exactly as :func:`tag_followed` and
    :func:`marker_miss_with_delivery` are: it feeds no design-doc section 7
    condition, no :func:`compute_verdicts` input, no cutoff and no
    :class:`GroupStats` win/loss field -- only its own ``follow_hop_rate``
    column.
    """
    if probe.probe_class != "follow_through":
        return None
    pages_by_uid = {page.uid: page for page in corpus.pages}
    hop_uids = deep_hop_uids(probe, pages_by_uid)
    if not hop_uids:
        return None
    delivered = frozenset(_delivered_uids(record, probe, corpus))
    return all(uid in delivered for uid in hop_uids)


def grade_correctness(record: RolloutRecord, probe: Probe, corpus: Corpus) -> bool | None:
    """Did *record*'s answer get *probe*'s ground truth right?

    Rewritten for issue athenaeum#1831 (operator ruling on athenaeum#1791
    comment 5732689494: "If the answer is right, it is right. Grade the
    answer on content, and establish which pages were used from evidence we
    already have rather than from a quoted tag."). Correctness is now
    CONTENT match plus DELIVERED-PAGE evidence, never the citation tag --
    see :func:`tag_followed` for what became of the old tag rule.

    Non-abstention: for every ``expected_uids`` page that is ANSWER-BEARING
    (carries one of ``probe.answer_tokens`` -- see
    :func:`tests.evals.corpus.answer_bearing_uids`; a
    ``disambiguation``/``distractor_robustness`` probe's context pages, a
    ``redundancy`` probe's non-planting duplicates, and a
    ``contradiction``/``negative_knowledge`` probe's STALE page are
    ``expected_uids`` without being answer-bearing, and are NOT required
    here -- exactly as ``answer_tokens`` already excluded them), BOTH of the
    following must hold:

    (a) at least one of that page's planted ``answer_markers`` values -- the
        fact itself, never the tag -- is present in the answer, compared
        under :func:`tests.evals.corpus._normalize_marker_for_match`
        (lowercase plus collapsed whitespace, issue athenaeum#1843) on BOTH
        sides. A uid may plant several ALTERNATIVE markers; any one of them
        satisfies this clause, which is how a marker the ceiling arm cannot
        reproduce verbatim gets repaired without loosening the grader. AND
    (b) that page's uid is in :func:`_delivered_uids` for this arm/record
        (the transcript's own recall/read-entity/file-read/breadcrumb
        evidence -- never merely ``expected_uids``, so a guessed or leaked
        marker with no delivery evidence still grades wrong, the same leak
        guard the old tag rule enforced).

    A ``multi_hop``/``follow_through`` probe whose planted facts split
    across two answer-bearing pages therefore needs BOTH pages' markers
    present AND BOTH pages delivered -- one page delivered, one not, grades
    ``False`` (the design doc's required counter-example 4).

    Returns ``None`` (never ``False``) when the probe carries no
    ``answer_tokens`` at all, or (defensively -- :func:`validate_core`
    already refuses to ship this) an answer-bearing page has no
    ``answer_markers`` entry -- a corpus authoring gap, not a graded miss.

    Abstention: graded by :func:`_is_abstention` (issue athenaeum#1836) --
    correct only when the answer asserts none of the corpus's planted
    tokens or markers AND uses recognizable declining language -- see the
    section docstring above.
    """
    # The answer_markers conjunct gets its OWN normalized copy of the answer
    # (issue athenaeum#1843), via the scoped marker normalizer rather than
    # the shared `_normalize_for_match` this function used to bind here: that
    # one is shared with the abstention/tag/harm/coverage graders and must
    # stay lowercase-only, while a marker phrase has to survive a line wrap
    # on either side. The old shared binding had no other reader left here,
    # so it is gone rather than left dead.
    marker_answer = _normalize_marker_for_match(record.answer)
    if probe.probe_class == "abstention":
        return _is_abstention(record.answer, probe, corpus)
    if not probe.answer_tokens:
        return None
    pages_by_uid = {page.uid: page for page in corpus.pages}
    required_uids = answer_bearing_uids(probe, pages_by_uid)
    if not required_uids:
        return None
    markers_by_uid: dict[str, list[str]] = {}
    for uid, marker in probe.answer_markers:
        markers_by_uid.setdefault(uid, []).append(marker)
    delivered = frozenset(_delivered_uids(record, probe, corpus))
    # Arm.NONE has no delivery channel at all -- by definition, not a gap in
    # one -- so it is graded on content alone, exactly as it always has been
    # (the old tag rule never gated NONE on delivery either). This is NOT
    # the same exemption the pre-athenaeum#1831 draft gave PUSH_BREADCRUMB/
    # NATIVE_INDEX (both have real per-page evidence now -- see
    # _breadcrumb_delivered_uids/_native_loaded_uids -- so both stay gated):
    # weak_probes below depends on NONE staying gradable-True on content so
    # it can keep catching a floor leak (prior knowledge/a guessable
    # marker), which is precisely what content-only grading is FOR here.
    content_only = record.arm is Arm.NONE
    for uid in required_uids:
        markers = markers_by_uid.get(uid, ())
        if not markers:
            return None
        if not content_only and uid not in delivered:
            return False
        if not any(
            _normalize_marker_for_match(marker) in marker_answer for marker in markers
        ):
            return False
    return True


def grade_harm(record: RolloutRecord, probe: Probe) -> bool | None:
    """Did *record*'s answer avoid every one of *probe*'s ``forbidden_tokens``?

    Wave-2 mechanism only (issue athenaeum#1772, athenaeum#1791 §2.1's
    ``report_only`` ruling) -- ``forbidden_tokens`` is empty for every probe
    class shipped so far, so this grades ``None`` on the current corpus.
    Feeds no §7 condition: ``compute_verdicts`` is unchanged by this issue.
    Enrollment of a future forbidden-token probe class into any decision is
    a distinct, explicit operator ruling (design doc §7 note, issue
    athenaeum#1776), never derived here.

    Same normalizer as :func:`grade_correctness` -- normalized substring
    match, no LLM judge. Deliberately does NOT consult
    :func:`_all_answer_tokens` -- that inventory has nothing to do with
    harm (it stopped being an abstention deny-list in athenaeum#1836 and
    is kept for the isolation pins in the test suite).

    Returns ``None`` (never ``False``) when ``probe.forbidden_tokens`` is
    empty -- a probe with nothing forbidden to say has no harm outcome to
    report, not a clean bill of health. Returns ``True`` when the answer
    contains none of ``probe.forbidden_tokens``, ``False`` when it contains
    any.
    """
    if not probe.forbidden_tokens:
        return None
    answer = _normalize_for_match(record.answer)
    return not any(_normalize_for_match(tok) in answer for tok in probe.forbidden_tokens)


def grade_coverage(record: RolloutRecord, probe: Probe) -> float | None:
    """What fraction of *probe*'s ``answer_tokens`` appear in *record*'s answer?

    Wave-2 mechanism only (issue athenaeum#1773, athenaeum#1791 §2.1's
    ``report_only`` ruling). Feeds no §7 condition: ``compute_verdicts`` is
    unchanged by this issue, and ``coverage_rate`` is not wired into it.
    Enrollment of a future many-correct-answer probe class (``aggregation``,
    item F) into any decision is a distinct, explicit operator ruling, never
    derived here.

    Same normalizer and substring instrument as :func:`grade_correctness`,
    but returns the MATCHED FRACTION rather than collapsing to a boolean --
    ``grade_correctness`` requires every token present or scores 0, which
    forces a many-correct-answer probe (naming 7 of 9 correct pages) through
    an all-or-nothing rule that measures nothing for that class.

    Returns ``None`` (never ``0.0``) when ``probe.answer_tokens`` is empty --
    the abstention probe class carries no ``answer_tokens`` by construction
    (nothing in the corpus answers it), so this is "no tokens to cover", not
    a graded zero.
    """
    if not probe.answer_tokens:
        return None
    answer = _normalize_for_match(record.answer)
    matched = sum(1 for tok in probe.answer_tokens if _normalize_for_match(tok) in answer)
    return matched / len(probe.answer_tokens)


def grade_marker_resolution(
    record: RolloutRecord, probe: Probe, corpus: Corpus
) -> bool | None:
    """Does every footnote marker the answer CITES resolve to a planted source?

    The optional follow-through check issue athenaeum#1730 adds to the
    ``follow_through`` probe class (issue athenaeum#1725). ``recall`` now
    renders, and compiled pages now carry, inline ``[^src-N]`` markers naming
    the source of the sentence they sit on; the question this grades is
    whether an answer that quotes a marker quoted a REAL one — a marker that
    resolves to a footnote definition on the page that planted the answer
    token — rather than inventing a citation-shaped string.

    **Recorded only.** Not a floor, and deliberately not wired into
    :func:`compute_verdicts`: it feeds no §7 condition, exactly like
    :func:`grade_harm` and :func:`grade_coverage`. Enrolling it in a decision
    is a distinct, explicit operator ruling (design doc §7 note, issue
    athenaeum#1776), never derived here.

    Returns ``None`` — never ``False`` — in the two "nothing to grade" cases,
    which is the whole reason this can ship on a corpus that plants no
    markers yet:

    * the answer cites no marker at all (a model that never cites cannot mis-cite,
      and grading that as a failure would score every arm zero today); and
    * no expected page that plants an answer token defines any footnote, so
      there is no ground truth to resolve against.

    ``True`` when every cited label has a definition on such a page; ``False``
    when any cited label has none.
    """
    cited = {m.group(1) for m in INLINE_MARKER_RE.finditer(record.answer)}
    if not cited:
        return None
    pages_by_uid = {page.uid: page for page in corpus.pages}
    definitions: dict[str, str] = {}
    for uid in probe.expected_uids:
        page = pages_by_uid.get(uid)
        if page is None:
            continue
        if probe.answer_tokens and not any(tok in page.body for tok in probe.answer_tokens):
            continue
        definitions.update(parse_footnote_definitions(page.body))
    if not definitions:
        return None
    return all(label in definitions for label in cited)


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
            text = _tool_result_content_text(block.get("content"))
            if text:
                parts.append(text)
    return "\n\n".join(parts)


def _tool_result_content_text(content: object) -> str:
    """The text payload of ONE ``tool_result`` block's ``content`` field.

    Factored verbatim out of :func:`_pull_delivered_text` (issue
    athenaeum#1842) so the ``read_entity`` pairing below decodes exactly
    the same two shapes the Claude Code stream-json format uses -- a plain
    string, or a list of ``{"type": "text", "text": ...}`` blocks -- rather
    than a second, independently-drifting copy of that logic. Joining a
    multi-block list with ``"\n\n"`` here reproduces the flat ``parts``
    list the caller used to build: both flatten to the same
    ``"\n\n"``-separated string. The ONE deliberate difference is that the
    caller now skips a wholly-EMPTY result rather than contributing a blank
    element to its own join (which used to widen the separator to
    ``"\n\n\n\n"``). Inert on real data -- verified byte-identical
    ``delivered_text_for_utilization`` output across all 1440 cells of the
    two stored runs re-graded for athenaeum#1842 -- and strictly more
    correct where it is not.

    Returns ``""`` (never raises) for any other shape, including ``None``.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n\n".join(
            str(sub.get("text", ""))
            for sub in content
            if isinstance(sub, dict) and sub.get("type") == "text"
        )
    return ""


def _is_read_entity_tool(name: object) -> bool:
    """Is *name* the athenaeum ``read_entity`` MCP tool?

    The transcripts spell it namespaced -- ``mcp__athenaeum__read_entity``
    (:data:`tests.evals.rollout.READ_ENTITY_TOOL_NAME`, which is how FastMCP
    names it and how both the api-mode tool schema and the CLI arm's
    ``--allowedTools`` list refer to it) -- so a bare ``== "read_entity"``
    equality would match nothing in a real store. Matched on the LAST
    ``__``-delimited segment rather than a loose ``"read_entity" in name``
    substring scan: the segment test accepts both the namespaced spelling
    and a bare ``read_entity`` (what a hand-built test transcript and any
    future un-namespaced transport would carry), while still refusing a
    DIFFERENT tool whose name merely contains the string (a hypothetical
    ``read_entity_batch`` or ``bulk_read_entity_v2`` returns a shape this
    function has never validated, and must not be silently counted as
    delivery evidence).
    """
    if not isinstance(name, str):
        return False
    return name.rsplit("__", 1)[-1] == "read_entity"


def _read_entity_delivered_uids(record: RolloutRecord) -> tuple[str, ...]:
    """Uids of pages the model actually fetched with the ``read_entity`` MCP
    tool in *record*'s transcript (issue athenaeum#1842).

    ``read_entity`` is the tool ``recall``'s own ``**Uid:**`` field exists
    to enable (``src/athenaeum/mcp_server.py``'s ``read_entity`` docstring),
    so a hop taken through that designed path is the STRONGEST delivery
    evidence in the suite -- but it returns a JSON object, not the
    ``recall_search`` markdown :func:`~tests.evals.metrics.uids_from_recall_output`
    parses, so before this issue such a page was invisible to
    :func:`_delivered_uids` and the cell graded as if the second hop had
    never happened.

    Walks the SAME transcript shape :func:`_pull_delivered_text` does, and
    PAIRS each call with its result: a ``tool_use`` block (``id``, ``name``,
    ``input.uid``) in an assistant event against the ``tool_result`` block
    whose ``tool_use_id`` matches, in a later user event. A uid is counted
    only when ALL of the following hold, so a REQUEST alone is never
    delivery evidence:

    * the result is present and not ``is_error``;
    * its content decodes as a JSON object;
    * that object's own ``uid`` equals the uid that was requested (the
      server resolves aliases, so a payload naming a DIFFERENT page is a
      redirect, not delivery of the page asked for); and
    * its ``body`` is a non-empty string -- an empty body delivered no
      content to read a planted marker out of.

    Order-independent (requests and results are collected in one pass, then
    joined), deduplicated, and never raises: any transcript that does not
    carry this shape yields ``()``, which is the common case for every
    rollout that never called the tool.
    """
    requested: dict[str, str] = {}
    results: dict[str, tuple[bool, str]] = {}
    for event in record.transcript:
        if not isinstance(event, dict):
            continue
        message = event.get("message")
        if not isinstance(message, dict):
            continue
        for block in message.get("content") or []:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if block_type == "tool_use":
                if not _is_read_entity_tool(block.get("name")):
                    continue
                call_id = block.get("id")
                tool_input = block.get("input")
                if not isinstance(call_id, str) or not isinstance(tool_input, dict):
                    continue
                uid = tool_input.get("uid")
                if isinstance(uid, str) and uid:
                    requested[call_id] = uid
            elif block_type == "tool_result":
                call_id = block.get("tool_use_id")
                if not isinstance(call_id, str):
                    continue
                results[call_id] = (
                    bool(block.get("is_error")),
                    _tool_result_content_text(block.get("content")),
                )

    delivered: list[str] = []
    for call_id, uid in requested.items():
        result = results.get(call_id)
        if result is None:
            continue
        is_error, text = result
        if is_error or not text:
            continue
        try:
            payload = json.loads(text)
        except (ValueError, TypeError):
            continue
        if not isinstance(payload, dict) or payload.get("uid") != uid:
            continue
        body = payload.get("body")
        if isinstance(body, str) and body.strip():
            delivered.append(uid)
    return tuple(dict.fromkeys(delivered))


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

    Deliberately NOT :func:`_delivered_uids` and deliberately not sharing a
    dispatch with it (issue athenaeum#1842 -- a prior version of
    ``_delivered_uids``'s docstring wrongly claimed otherwise). This
    function answers "what was PUT in front of the model, paid for whether
    wanted or not", which is what a WASTE figure must be computed over; the
    grader's function answers "what evidence is there that a page reached
    the model", which legitimately includes a page the model fetched itself
    via ``read_entity``. Counting a self-fetched page as waste basis would
    be wrong -- the model asked for it -- so athenaeum#1842's read_entity
    channel was added to the grader ONLY, leaving every ``uid_citation_rate``
    and ``mean_wasted_*`` figure byte-identical.
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

    # Harm (issue athenaeum#1772, report_only wave-2 mechanism) -- all arms;
    # computed only over rows whose probe carries forbidden_tokens. None on
    # the current corpus (no probe class plants any yet) for every group --
    # feeds no §7 condition.
    harm_free_rate: float | None

    # Coverage (issue athenaeum#1773, report_only wave-2 mechanism) -- all
    # arms; mean of grade_coverage's per-row fraction over gradable rows
    # (probe carries answer_tokens). Feeds no §7 condition.
    coverage_rate: float | None

    # Marker resolution (issue athenaeum#1730, report_only) -- all arms;
    # computed only over rows whose answer cites a footnote marker AND whose
    # expected token-bearing pages define one. None on a corpus that plants
    # no markers, for every group. Feeds no §7 condition.
    marker_resolution_rate: float | None

    # Index coverage (issue athenaeum#1725, NATIVE_INDEX only) -- the
    # fraction of the corpus the TRUNCATED index still names, read back from
    # what Claude Code actually loaded. None (never 0.0) for every other arm
    # -- "no index to speak of" is a different fact from "0% coverage".
    mean_index_coverage: float | None

    # Reference tag followed (issue athenaeum#1831, report_only) -- mean of
    # tag_followed's per-row bool over rows whose probe carries
    # answer_tokens (non-abstention). Feeds no section 7 condition and no
    # win/loss: correctness_rate above is graded on answer_markers now,
    # never this. None when no row in the group carries a gradable value.
    tag_followed_rate: float | None

    # Marker miss with delivery (issue athenaeum#1842, report_only) -- the
    # COUNT (not a rate: the absolute number is what a reader acts on) of
    # gradable cells in the group where every answer-bearing page WAS
    # delivered and at least one of them contributed no matching marker --
    # see marker_miss_with_delivery. Feeds no section 7 condition and no
    # win/loss field, exactly as tag_followed_rate above does not. None
    # (never 0) when no row in the group is gradable at all -- "nothing to
    # count" is a different fact from "counted, found none". Appended last
    # so existing positional construction and sibling-lane merges stay safe.
    marker_miss_with_delivery: int | None = None

    # Follow-through hop delivered (issue athenaeum#1844, report_only) --
    # among follow_through cells, the fraction where every page named by
    # `corpus.deep_hop_uids` was delivered to the arm; see
    # deep_hop_delivered. None (rendered `n/a`) for every other probe_class,
    # which has no hop to have followed -- a different fact from a rate of
    # zero. Feeds no section 7 condition, no cutoff and no win/loss field,
    # exactly as tag_followed_rate and marker_miss_with_delivery above do
    # not. Appended last so existing positional construction and
    # sibling-lane merges stay safe.
    follow_hop_rate: float | None = None


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
        harm_flags: list[float] = []
        coverage_values: list[float] = []
        marker_resolutions: list[float] = []
        index_coverages: list[float] = []
        tag_followed_flags: list[float] = []
        marker_miss_flags: list[float] = []
        follow_hop_flags: list[float] = []

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

            harm = grade_harm(record, probe)
            if harm is not None:
                harm_flags.append(1.0 if harm else 0.0)

            coverage = grade_coverage(record, probe)
            if coverage is not None:
                coverage_values.append(coverage)

            resolved = grade_marker_resolution(record, probe, corpus)
            if resolved is not None:
                marker_resolutions.append(1.0 if resolved else 0.0)

            followed = tag_followed(record, probe)
            if followed is not None:
                tag_followed_flags.append(1.0 if followed else 0.0)

            marker_miss = marker_miss_with_delivery(record, probe, corpus)
            if marker_miss is not None:
                marker_miss_flags.append(1.0 if marker_miss else 0.0)

            hop_delivered = deep_hop_delivered(record, probe, corpus)
            if hop_delivered is not None:
                follow_hop_flags.append(1.0 if hop_delivered else 0.0)

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
                harm_free_rate=_mean(harm_flags),
                coverage_rate=_mean(coverage_values),
                marker_resolution_rate=_mean(marker_resolutions),
                mean_index_coverage=(
                    _mean(index_coverages) if arm == Arm.NATIVE_INDEX.value else None
                ),
                tag_followed_rate=_mean(tag_followed_flags),
                marker_miss_with_delivery=(
                    int(sum(marker_miss_flags)) if marker_miss_flags else None
                ),
                follow_hop_rate=_mean(follow_hop_flags),
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

    # Issue athenaeum#1824: the other half of the measurement. Every field
    # above rewards REMEMBERING; these two measure whether the system had
    # the sense to FORGET. ``transient_total`` counts the distinct tokens
    # planted on ``retain=False`` observations (a temporary outage, a
    # point-in-time status, a task-scoped instruction) and
    # ``transient_retained`` how many of them the store kept -- so LOWER is
    # better here, and a system scoring 36/36 on retention while also
    # scoring 3/3 here is hoarding, not winning. Defaulted so every existing
    # construction site (and every sibling-store row written before this
    # field existed) stays valid; ``None`` means the stream carried no
    # transient observation at all, never a fabricated 0.
    transient_total: int = 0
    transient_retained: int | None = None

    # Issue athenaeum#1830 (AC "name the lost facts"): the DURABLE answer
    # tokens that were targeted (``token_bearing`` observations) but not
    # found anywhere in *store_files* -- i.e. exactly the tokens missing
    # from ``answer_tokens_retained``, named rather than left as a bare
    # count. Sorted for determinism; defaulted to ``()`` so every existing
    # construction site and every sibling-store row written before this
    # field existed still loads.
    lost_token_ids: tuple[str, ...] = ()

    # Issue athenaeum#1841: decay CORRECTNESS, not decay presence. The
    # athenaeum#1830 rule above grades a transient token correct as soon as
    # SOME short decay was picked, so it cannot tell ``daily`` from
    # ``weekly`` and it is blind to the opposite failure -- a durable fact
    # filed to expire. These two close both gaps, scored against the same
    # ``transient_total`` / durable-token populations the columns above use.
    #
    # ``decay_correct`` counts the transient tokens filed under EXACTLY the
    # bucket their observation's :attr:`~tests.evals.corpus.Observation.
    # expected_bucket` names (higher is better, out of ``transient_total``).
    # ``None`` -- never a fabricated 0, the same convention
    # ``transient_retained`` holds -- when the measurement is not available
    # at all: no transient token carries an expectation, or the store
    # declares no decay-bucket frontmatter anywhere (the native arm, whose
    # memory files have no frontmatter vocabulary to read). A store that
    # DOES speak buckets and simply got them wrong reads 0, which is a
    # measurement, not a fabrication.
    #
    # ``durable_overdecayed`` counts the ``retain=True`` answer tokens that
    # landed on a page filed under a bucket that EXPIRES -- the over-decay
    # failure the operator weights equally with under-decay. LOWER is
    # better, and a plain ``int`` (not ``int | None``) because "no durable
    # token was over-decayed" and "no durable token was measurable" are
    # already told apart by ``answer_tokens_total``.
    decay_correct: int | None = None
    durable_overdecayed: int = 0


#: Decay buckets (:data:`athenaeum.models.MEMORY_BUCKETS`) short enough that
#: filing a transient observation under one is a REASONABLE decay, not a
#: durable claim (issue athenaeum#1830, operator ruling: "keeping a
#: transient fact is only wrong if it is filed without a reasonable decay").
#: ``"durable"`` is deliberately excluded -- it is the one bucket whose own
#: name says the opposite of "temporary".
_SHORT_MEMORY_BUCKETS: frozenset[str] = frozenset({"daily", "weekly"})

#: How close a compiled page's ``valid_until`` must fall to the transient
#: observation's OWN timestamp to count as "near-term" (issue athenaeum#1830).
#: Anchored to the observation's own timestamp, never ``date.today()`` --
#: a grading rule that depends on when the suite happens to run is not a
#: reproducible measurement. No existing precedent constant covers this
#: window; two weeks is chosen as clearly shorter than the corpus's own
#: multi-month observation date range (``tests/evals/corpus.py``) while
#: still wide enough that an off-by-a-few-days ``valid_until`` is not
#: penalised as if it were durable.
_TRANSIENT_NEAR_TERM_DAYS = 14

#: The one :data:`athenaeum.models.MEMORY_BUCKETS` member that does NOT
#: expire (issue athenaeum#1841). Any other bucket on a page carrying a
#: ``retain=True`` token is an over-decay: a durable fact filed to be swept.
#: Named rather than inverted from :data:`_SHORT_MEMORY_BUCKETS` so the
#: over-decay scan stays correct if the vocabulary grows a fourth, longer
#: horizon -- a durable fact filed under it would still be wrong.
_DURABLE_MEMORY_BUCKET = "durable"


def _store_page_buckets(store_files: Mapping[str, str]) -> dict[str, str]:
    """``{path: bucket}`` for every page in *store_files*, ``""`` where the
    page declares none (issue athenaeum#1841).

    The second pass the plan calls for: the durable-retention scan in
    :func:`compute_write_path_stats` joins the store into one string and
    never opens frontmatter, and :func:`_transient_token_handled_correctly`
    re-parses per call. Parsing once here keeps the two new columns from
    re-reading every page per token, and gives
    :func:`compute_write_path_stats` the store-level "does this store speak
    buckets at all" fact its ``None`` convention turns on.
    """
    return {path: parse_bucket(parse_frontmatter(text)[0]) for path, text in store_files.items()}


def _transient_decay_bucket_correct(
    token: str,
    *,
    expected_bucket: str,
    store_files: Mapping[str, str],
    page_buckets: Mapping[str, str],
) -> bool:
    """Grade one transient token's decay CORRECTNESS (issue athenaeum#1841).

    Correct when the token is present on at least one compiled page and
    EVERY page carrying it declares exactly *expected_bucket*. Deliberately
    stricter than :func:`_transient_token_handled_correctly` in both
    directions, and the difference is the point of the column:

    * a token filed ``weekly`` when ``daily`` was expected grades correct
      there (``weekly`` is a reasonable decay) and WRONG here;
    * a token absent from the store entirely grades correct there (it was
      discarded, which the athenaeum#1830 ruling calls a fine outcome) and
      NOT correct here -- no decay was picked, so no decay can be right.
      Absence is scored by ``transient_retained``, which is why both columns
      are reported side by side rather than one replacing the other.

    A token with no recorded expectation (``expected_bucket == ""``) is not
    gradeable and never counts correct; the corpus records one for every
    transient it plants (``tests/evals/corpus.py``), so this is a guard
    against a hand-built stream, not a live case.
    """
    if not expected_bucket:
        return False
    found = False
    for path, text in store_files.items():
        if token not in text:
            continue
        found = True
        if page_buckets.get(path, "") != expected_bucket:
            return False
    return found


def _observation_timestamp(observation: Observation) -> datetime | None:
    """Parse :attr:`Observation.timestamp` (``%Y%m%dT%H%M%SZ``, see that
    field's own ``filename`` property) into a ``datetime``, or ``None`` for
    a value that does not match -- fail-open, mirroring every frontmatter
    reader in :mod:`athenaeum.models` this function's callers already use."""
    try:
        return datetime.strptime(observation.timestamp, "%Y%m%dT%H%M%SZ")
    except ValueError:
        return None


def _transient_token_handled_correctly(
    token: str,
    *,
    anchor: datetime | None,
    store_files: Mapping[str, str],
) -> bool:
    """Grade one transient (``retain=False``) token per the issue athenaeum#1830
    operator ruling: correct when the token is ABSENT from every compiled
    page, or present only on page(s) that all carry a short decay bucket
    (:data:`_SHORT_MEMORY_BUCKETS`) or a ``valid_until`` within
    :data:`_TRANSIENT_NEAR_TERM_DAYS` of the observation's own timestamp.
    Wrong only when at least one page carrying the token is filed with
    neither -- i.e. filed as durable.

    A page's frontmatter is read PER PAGE, not off a joined corpus string
    (unlike the durable-retention scan below), because bucket/valid_until
    are page-level facts :func:`athenaeum.models.parse_frontmatter` can only
    read from one page's own text. A token that landed on more than one page
    must be handled correctly on ALL of them to grade correct -- one durable
    copy is still a durable filing of the fact.
    """
    for text in store_files.values():
        if token not in text:
            continue
        meta, _body = parse_frontmatter(text)
        bucket = parse_bucket(meta)
        if bucket in _SHORT_MEMORY_BUCKETS:
            continue
        valid_until_raw = validity_bound_str(meta, "valid_until")
        if valid_until_raw and anchor is not None:
            try:
                valid_until = datetime.strptime(valid_until_raw, "%Y-%m-%d")
            except ValueError:
                valid_until = None
            if valid_until is not None and valid_until - anchor <= timedelta(
                days=_TRANSIENT_NEAR_TERM_DAYS
            ):
                continue
        # Neither a short bucket nor a near-term valid_until on a page that
        # carries the token -- filed as durable, which is the only case the
        # operator ruling calls wrong.
        return False
    # Absent everywhere, or present only on page(s) that all graded short/
    # near-term above.
    return True


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

    Issue athenaeum#1824: ``retain=False`` (transient) observations are
    scored SEPARATELY and are excluded from every retention field above.
    Folding them in would corrupt all five at once -- ``answer_tokens_total``
    and ``observations_measured`` inflate, ``pages_targeted`` grows pages
    (``transient-*`` sentinels) that were never meant to exist, and a store
    that correctly discarded an outage note would be scored as having
    dropped a fact. They get their own pair instead: ``transient_total`` and
    ``transient_retained``, where a LOW retained count is the good result.

    Issue athenaeum#1830 (operator ruling): a transient token is graded
    WRONG only when it is filed DURABLY -- present on a compiled page with
    neither a short decay bucket nor a near-term ``valid_until``
    (:func:`_transient_token_handled_correctly`). An absent token, or one
    filed with a reasonable decay, grades correct. This is why the transient
    half of this function reads *store_files* PER PAGE (for frontmatter)
    rather than joined into ``corpus_text`` like the durable half above,
    which never needed frontmatter at all.

    Issue athenaeum#1841 adds decay CORRECTNESS on top of that ruling,
    without changing it: ``decay_correct`` counts the transient tokens filed
    under exactly their observation's ``expected_bucket``, and
    ``durable_overdecayed`` counts the durable tokens filed under a bucket
    that expires. Both read the per-page frontmatter parsed once into
    :func:`_store_page_buckets`, a second pass over *store_files* that the
    retention half above never needs.
    """
    corpus_text = "\n".join(store_files.values())
    page_buckets = _store_page_buckets(store_files)
    token_bearing = [obs for obs in observations if obs.answer_tokens and obs.retain]
    transient_bearing = [obs for obs in observations if obs.answer_tokens and not obs.retain]
    transient_tokens = sorted({token for obs in transient_bearing for token in obs.answer_tokens})
    # Anchor each transient token to the timestamp of the (single)
    # observation that planted it -- corpus.py's transient observations are
    # one token each, so this is an exact, not approximate, mapping.
    transient_anchor: dict[str, datetime | None] = {}
    for obs in transient_bearing:
        anchor = _observation_timestamp(obs)
        for token in obs.answer_tokens:
            transient_anchor[token] = anchor
    transient_wrong = {
        token
        for token in transient_tokens
        if not _transient_token_handled_correctly(
            token, anchor=transient_anchor.get(token), store_files=store_files
        )
    }
    # Issue athenaeum#1841: the bucket each transient token SHOULD carry.
    # Same exact one-token-per-observation mapping ``transient_anchor``
    # relies on, so no expectation is silently overwritten.
    transient_expected: dict[str, str] = {}
    for obs in transient_bearing:
        for token in obs.answer_tokens:
            transient_expected[token] = obs.expected_bucket
    store_declares_bucket = any(page_buckets.values())
    decay_correct: int | None = None
    if any(transient_expected.values()) and store_declares_bucket:
        decay_correct = sum(
            1
            for token in transient_tokens
            if _transient_decay_bucket_correct(
                token,
                expected_bucket=transient_expected.get(token, ""),
                store_files=store_files,
                page_buckets=page_buckets,
            )
        )

    all_tokens = sorted({token for obs in token_bearing for token in obs.answer_tokens})
    retained_tokens = {token for token in all_tokens if token in corpus_text}
    lost_tokens = sorted(set(all_tokens) - retained_tokens)

    targeted_pages = sorted({obs.page_uid for obs in token_bearing})
    written_pages = {
        obs.page_uid
        for obs in token_bearing
        if any(token in corpus_text for token in obs.answer_tokens)
    }
    dropped = [
        obs for obs in token_bearing if not all(token in corpus_text for token in obs.answer_tokens)
    ]
    # Issue athenaeum#1841 (over-decay): a DURABLE token that landed on a
    # page filed under any bucket other than ``durable`` is a fact the store
    # has scheduled to forget. Counted per distinct token, not per page, to
    # stay on the same grain as ``answer_tokens_retained`` beside it.
    overdecayed_pages = {
        path for path, bucket in page_buckets.items() if bucket and bucket != _DURABLE_MEMORY_BUCKET
    }
    durable_overdecayed = sum(
        1 for token in all_tokens if any(token in store_files[path] for path in overdecayed_pages)
    )

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
        transient_total=len(transient_tokens),
        transient_retained=len(transient_wrong) if transient_tokens else None,
        lost_token_ids=tuple(lost_tokens),
        decay_correct=decay_correct,
        durable_overdecayed=durable_overdecayed,
    )


# ---------------------------------------------------------------------------
# Filing loss (issue athenaeum#1830 AC2) -- distinct from
# ``compute_write_path_stats`` above: that scores the compiled store against
# every observation Claude was ever GIVEN; this scores it against the
# tokens Claude's OWN native writer chose to RETAIN in its memory files, so
# the filing step's own loss is separable from Claude's write judgement.
# See the AC's own counter-example: native drops token X, the librarian
# keeps everything else it was handed -- filing loss 0, total loss 1.
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class FilingLossStats:
    """"Claude's memories versus Claude's memories after filing" (issue
    athenaeum#1830 operator ruling) -- the second, separate retention row
    the AC requires beside :class:`WritePathStats`.

    ``native_tokens_total`` is the count of durable (``retain=True``)
    planted answer tokens found anywhere in the NATIVE writer's own
    ``memory_files`` -- the denominator, i.e. what Claude itself chose to
    keep before any filing happened. ``filed_tokens_retained`` is how many
    of those survive in the LIBRARIAN-compiled store; ``None`` only when
    ``native_tokens_total`` is 0 (nothing to measure, never a fabricated
    0/0). ``lost_token_ids`` names the ones that did not survive filing.
    """

    system: str
    corpus_scale: str
    native_tokens_total: int
    filed_tokens_retained: int | None
    lost_token_ids: tuple[str, ...] = ()


def compute_filing_loss_stats(
    system: str,
    corpus_scale: str,
    observations: Sequence[Observation],
    native_files: Mapping[str, str],
    compiled_store_files: Mapping[str, str],
) -> FilingLossStats:
    """Score *compiled_store_files* (the librarian's compile of
    *native_files*) against the subset of durable planted tokens *native_files*
    itself already retained -- see :class:`FilingLossStats`.

    A token Claude never wrote down at all is excluded from the denominator
    entirely: it is a native WRITE loss (already visible on the ordinary
    :func:`compute_write_path_stats` row for the ``native`` system), not a
    FILING loss -- the librarian was never given the chance to keep it.
    """
    native_text = "\n".join(native_files.values())
    compiled_text = "\n".join(compiled_store_files.values())
    token_bearing = [obs for obs in observations if obs.answer_tokens and obs.retain]
    all_tokens = sorted({token for obs in token_bearing for token in obs.answer_tokens})
    native_retained = sorted(token for token in all_tokens if token in native_text)
    filed_retained = {token for token in native_retained if token in compiled_text}
    lost = sorted(set(native_retained) - filed_retained)
    return FilingLossStats(
        system=system,
        corpus_scale=corpus_scale,
        native_tokens_total=len(native_retained),
        filed_tokens_retained=len(filed_retained) if native_retained else None,
        lost_token_ids=tuple(lost),
    )


# ---------------------------------------------------------------------------
# Cost per correct answer, the cost-ratio decision reading, and the three
# go/no-go verdicts (issue athenaeum#1734, design lock
# docs/design/native-memory-baseline.md §6/§7).
#
# Everything above measures ONE dimension at a time. This section reads
# several of them together to answer the design doc's actual question: at
# this scale, does Athenaeum win the relationship use case, not lose
# anywhere else, and stay within budget -- and if so, starting at which
# scale.
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class WriteCost:
    """Phase 2 write-path SPEND for one system at one corpus scale -- an
    independent, pre-computed input, exactly like ``write_path_stats`` on
    :func:`build_report` (see that function's own docstring for why: it
    cannot be derived from a :class:`~tests.evals.containment.ResultStore`
    here, since it is scored against the compile run itself, not a probe
    rollout).

    Distinct from :class:`WritePathStats`: that measures retention accuracy
    (which planted tokens survived compilation). This measures what the
    compile run COST, in raw input+output tokens, for
    :func:`compute_cost_per_correct` to amortise over the probe set at the
    same scale (design doc §6 "Write cost, both systems"). ``system`` is
    ``"athenaeum"`` (the librarian's compile spend) or ``"native"`` (the
    writer sessions' memory-saving tool-call spend, summed across
    ``NativeWriterResult.sessions[*].turn_tokens``).
    """

    system: str
    corpus_scale: str
    input_tokens: int
    output_tokens: int

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


def _system_for_arm(arm: str) -> str:
    """``"native"`` for the two native arms, ``"athenaeum"`` for every
    other arm (including ``none``/``oracle``, which still read whatever
    store the librarian compiled)."""
    return "native" if arm in _NATIVE_ARM_VALUES else "athenaeum"


@dataclasses.dataclass(frozen=True)
class CostPerCorrect:
    """Cost per correct answer for one (probe_class, corpus_scale, arm)
    group: total tokens for the cell divided by correct answers in the
    cell (issue athenaeum#1734 AC1).

    ``cost_per_correct`` is ``None`` -- never ``inf`` or ``0`` -- when
    ``correct_n == 0``; ``undefined_reason`` names why. Phase 2 fields
    (``write_tokens_amortized``/``write_tokens_raw``) are ``None`` when no
    :class:`WriteCost` was supplied for this group's ``(system,
    corpus_scale)``, which is the Phase-1-only case (AC3).
    """

    probe_class: str
    corpus_scale: str
    arm: str
    n: int
    correct_n: int
    read_input_tokens: int
    read_output_tokens: int
    # Phase 2 only (issue athenaeum#1726 landed the generator; None means no
    # WriteCost was supplied for this group's (system, corpus_scale)):
    # write_tokens_amortized = write_cost.total_tokens / probe_count_at_scale
    # * this-cell's row count -- design doc §6's "amortised over the full
    # probe set at that scale". write_tokens_raw is the SAME WriteCost's
    # total_tokens, unamortised, printed alongside per the design doc.
    write_tokens_amortized: float | None
    write_tokens_raw: int | None
    cost_per_correct: float | None
    undefined_reason: str | None


def compute_cost_per_correct(
    rows: Sequence[RolloutRow],
    *,
    write_costs: Sequence[WriteCost] = (),
    probe_counts: Mapping[str, int] | None = None,
) -> list[CostPerCorrect]:
    """Cost per correct answer, per (probe_class, corpus_scale, arm) group.

    ``cost_per_correct = (read_tokens + write_tokens_amortized) /
    correct_n`` where ``read_tokens`` is the group's summed
    ``turn_tokens`` (input + output, across every row and every turn in the
    cell -- read cost only, Phase 1) and ``write_tokens_amortized`` is
    ``0`` when no matching :class:`WriteCost` exists (Phase 1) or
    ``write_cost.total_tokens / probe_count_at_scale * len(group)`` when
    one does (Phase 2: the SAME per-probe amortised share this cell's rows
    would draw from the whole probe set, design doc §6). ``None`` (never
    ``inf``/``0``) when ``correct_n == 0`` -- a zero-correct cell has no
    denominator to divide by, and that is a fact worth stating, not hiding
    behind a fabricated number (issue athenaeum#1734 AC2).

    *write_costs* maps a ``(system, corpus_scale)`` pair to its spend via
    :func:`_system_for_arm` -- Athenaeum arms (everything but the two
    native arms) draw on the ``"athenaeum"`` entry, ``native_index``/
    ``native_grep`` draw on the ``"native"`` entry. *probe_counts*
    overrides the probe-set size used for amortisation (keyed by
    ``corpus_scale``) -- for unit tests that hand-build rows without
    generating a real ``medium``/``large`` corpus; ``None`` (the default)
    derives it from ``len(_corpus_for_scale(scale).probes)``, exact because
    every rollout at a given scale was generated from the same corpus
    (see :data:`_CORPUS_CACHE`'s own note).
    """
    write_by_key = {(w.system, w.corpus_scale): w for w in write_costs}
    results: list[CostPerCorrect] = []
    for (probe_class, corpus_scale, arm), group in sorted(_group_rows(rows).items()):
        read_input = 0
        read_output = 0
        correct_n = 0
        for row in group:
            record = row.record
            read_input += sum(t.input_tokens for t in record.turn_tokens)
            read_output += sum(t.output_tokens for t in record.turn_tokens)
            probe = _probe_for_row(row)
            corpus = _corpus_for_scale(corpus_scale)
            if grade_correctness(record, probe, corpus):
                correct_n += 1

        write_cost = write_by_key.get((_system_for_arm(arm), corpus_scale))
        write_raw: int | None = None
        write_amortized: float | None = None
        if write_cost is not None:
            if probe_counts is not None:
                probe_count = probe_counts.get(corpus_scale, 0)
            else:
                probe_count = len(_corpus_for_scale(corpus_scale).probes)
            write_raw = write_cost.total_tokens
            if probe_count > 0:
                write_amortized = (write_cost.total_tokens / probe_count) * len(group)

        total_tokens = read_input + read_output + (write_amortized or 0.0)
        if correct_n == 0:
            cost_per_correct = None
            undefined_reason = "zero correct answers in this cell"
        else:
            cost_per_correct = total_tokens / correct_n
            undefined_reason = None

        results.append(
            CostPerCorrect(
                probe_class=probe_class,
                corpus_scale=corpus_scale,
                arm=arm,
                n=len(group),
                correct_n=correct_n,
                read_input_tokens=read_input,
                read_output_tokens=read_output,
                write_tokens_amortized=write_amortized,
                write_tokens_raw=write_raw,
                cost_per_correct=cost_per_correct,
                undefined_reason=undefined_reason,
            )
        )
    return results


def _reading_for_ratio(ratio: float | None) -> str:
    """Design doc §7's four-band reading: ``>2.0 fail``, ``<=2.0 limit``,
    ``<=1.0 target``, ``<=0.5 aspirational``. ``"undefined"`` (never a
    fabricated band) when *ratio* is ``None`` -- one side's cost per
    correct was itself undefined (zero correct answers)."""
    if ratio is None:
        return "undefined"
    if ratio <= 0.5:
        return "aspirational"
    if ratio <= 1.0:
        return "target"
    if ratio <= 2.0:
        return "limit"
    return "fail"


#: Ruling R1 (Quine review of PR#1740): all three §7 conditions evaluate the
#: SAME Athenaeum arm -- the shipped configuration (sidecar breadcrumbs plus
#: the recall tool), never "whichever delivery arm wins this condition".
#: Overridable per report via ``--verdict-arm`` (``north_star_cli.py``) /
#: ``build_report(verdict_arm=...)`` -- other Athenaeum arms still appear in
#: every per-dimension table above, just never in the verdicts. Defined
#: here, immediately above :func:`compute_cost_ratios` (its first use),
#: rather than a literal default repeated at each call site.
DEFAULT_VERDICT_ARM: str = Arm.PUSH_BREADCRUMB_PULL.value


@dataclasses.dataclass(frozen=True)
class CostRatio:
    """*verdict_arm*'s cost per correct answer against the BETTER (cheaper
    DEFINED cost) of the two native arms, at one (probe_class, corpus_scale)
    -- "better" in a cost sentence means the lower cost among natives that
    actually scored a correct answer, the harshest reading available for
    Athenaeum (issue athenaeum#1734 AC2). Orchestrator ruling R1: this is
    ONE PINNED Athenaeum arm, never the cheapest of the delivery arms --
    cherry-picking a different winning arm per condition is exactly what
    the decision rule must not do.

    ``ratio``/``reading`` are ``None``/``"undefined"`` when both sides'
    cost per correct could not be computed, or when *verdict_arm* itself
    scored zero correct answers (see ``detail``). ``reading ==
    "native-zero"`` (ruling R3) is the one case where an undefined RATIO
    still PASSES: the better native arm scored zero correct answers at
    this class/scale while *verdict_arm* scored at least one -- there is
    no ratio to compute, but the direction is unambiguous. ``native_present``
    (ruling R5) is ``False`` only when NEITHER native arm has ANY row in
    this group at all -- :func:`compute_verdicts` uses it to SKIP the class
    for condition 3 (mirroring condition 2's R4 skip), rather than failing
    the whole scale outright.
    """

    probe_class: str
    corpus_scale: str
    athenaeum_cost: float | None
    native_cost: float | None
    ratio: float | None
    reading: str
    detail: str
    native_present: bool


def compute_cost_ratios(
    costs: Sequence[CostPerCorrect], *, verdict_arm: str = DEFAULT_VERDICT_ARM
) -> list[CostRatio]:
    """Group *costs* by (probe_class, corpus_scale) and compare *verdict_arm*'s
    cost per correct against the best (cheapest, DEFINED) native-arm cost
    per correct.

    Ruling R1: *verdict_arm* is the ONE Athenaeum arm every condition
    reads -- never "the cheapest of the delivery arms" (that was the
    cherry-picking Quine's review caught: a different arm can win each
    condition, which is not a decision rule at all).

    Ruling R3: when native ran in this group (at least one
    ``native_index``/``native_grep`` :class:`CostPerCorrect` row exists)
    but every native arm scored zero correct answers, *and* *verdict_arm*
    scored at least one, this reads ``"native-zero"`` -- a PASS, stated in
    words rather than as a fabricated ratio. If *verdict_arm* ALSO scored
    zero, it is ``"undefined"`` (a fail): neither side has anything to
    compare.

    Ruling R5: when native did not run in this group AT ALL (no
    ``native_index``/``native_grep`` row at all, as opposed to native rows
    that scored zero), ``native_present`` is ``False`` -- :func:`compute_verdicts`
    treats this the same way condition 2 treats a class with no gradable
    rows on one side (ruling R4): SKIPPED, named, never a scale-level fail
    on its own.
    """
    by_group: dict[tuple[str, str], list[CostPerCorrect]] = defaultdict(list)
    for cost in costs:
        by_group[(cost.probe_class, cost.corpus_scale)].append(cost)

    results: list[CostRatio] = []
    for (probe_class, corpus_scale), group in sorted(by_group.items()):
        verdict_entries = [c for c in group if c.arm == verdict_arm]
        athenaeum_cost = (
            verdict_entries[0].cost_per_correct
            if verdict_entries and verdict_entries[0].cost_per_correct is not None
            else None
        )
        athenaeum_correct_n = verdict_entries[0].correct_n if verdict_entries else 0

        native_entries = [c for c in group if c.arm in _NATIVE_ARM_VALUES]
        native_present = bool(native_entries)
        native_defined_costs = [
            c.cost_per_correct for c in native_entries if c.cost_per_correct is not None
        ]
        native_cost = min(native_defined_costs) if native_defined_costs else None
        native_ran_but_scored_zero = native_present and native_cost is None

        if native_cost is not None and athenaeum_cost is not None:
            ratio = athenaeum_cost / native_cost
            reading = _reading_for_ratio(ratio)
            detail = ""
        elif native_ran_but_scored_zero and athenaeum_cost is not None:
            ratio = None
            reading = "native-zero"
            detail = (
                f"native bought zero correct answers at this class/scale; "
                f"{verdict_arm!r} bought {athenaeum_correct_n}"
            )
        elif native_ran_but_scored_zero and athenaeum_cost is None:
            ratio = None
            reading = "undefined"
            detail = "both sides bought zero correct answers at this class/scale"
        elif athenaeum_cost is None and native_cost is not None:
            ratio = None
            reading = "undefined"
            detail = f"{verdict_arm!r}'s cost per correct is undefined at this class/scale"
        else:
            ratio = None
            reading = "undefined"
            detail = "no native cost data at this class/scale"

        results.append(
            CostRatio(
                probe_class=probe_class,
                corpus_scale=corpus_scale,
                athenaeum_cost=athenaeum_cost,
                native_cost=native_cost,
                ratio=ratio,
                reading=reading,
                detail=detail,
                native_present=native_present,
            )
        )
    return results


#: The relationship use case (design doc, use-cases §2.1): these four probe
#: classes, filtered to the probes among them whose expected pages are
#: person or company type. A probe class here can straddle both sides of
#: the split -- e.g. ``multi_hop``'s ``ratecard_tooling_owner`` (a repo and
#: a tool page, neither person/company) is NOT in the subset while
#: ``spend_approver_named`` (a policy page plus a person page) IS -- so the
#: filter is applied per PROBE, never per whole class.
_RELATIONSHIP_PROBE_CLASSES = frozenset({"single_hop", "multi_hop", "disambiguation", "temporal"})
_RELATIONSHIP_PAGE_TYPES = frozenset({"person", "company"})

#: §7 gates the whole decision rule at "the `medium` scale and above" and is
#: explicit that "winning only below the cap is not a pass" -- so the
#: cutoff can never land on `core`/`small` even if one of them happens to
#: pass every condition. Sliced from :data:`SIZE_SCALE_ORDER` (the SIZE axis
#: only, same as :func:`crossover_scales`) starting at ``"medium"``.
_CUTOFF_ELIGIBLE_SCALES: tuple[str, ...] = SIZE_SCALE_ORDER[SIZE_SCALE_ORDER.index("medium") :]


def _pooled_correctness(rows: Sequence[RolloutRow], arm: str) -> tuple[int, int]:
    """``(correct, total)`` pooled across every gradable row in *rows* for
    *arm* -- ruling R2: condition 1 is ONE correctness rate over the whole
    relationship-probe set, never a max taken over per-probe-class rates.
    A row whose probe carries no ground truth to grade (``grade_correctness``
    returns ``None``) is excluded from both numerator and denominator, same
    as every other correctness figure in this module."""
    correct = 0
    total = 0
    for row in rows:
        if row.record.arm.value != arm:
            continue
        probe = _probe_for_row(row)
        corpus = _corpus_for_scale(row.record.corpus_scale)
        graded = grade_correctness(row.record, probe, corpus)
        if graded is None:
            continue
        total += 1
        if graded:
            correct += 1
    return correct, total


def _relationship_probe_ids(scales: Iterable[str]) -> frozenset[str]:
    """Derive the relationship-use-case probe id set from the real corpus
    at each of *scales* -- the default *rows*-need-no-corpus escape hatch
    in :func:`compute_verdicts` still reaches the corpus here, only when
    the caller did not already supply an explicit set (unit tests do, so
    they never need to build a real ``medium``/``large`` corpus)."""
    ids: set[str] = set()
    for scale in scales:
        corpus = _corpus_for_scale(scale)
        pages_by_uid = {page.uid: page for page in corpus.pages}
        for probe in corpus.probes:
            if probe.probe_class not in _RELATIONSHIP_PROBE_CLASSES:
                continue
            if any(
                pages_by_uid[uid].type in _RELATIONSHIP_PAGE_TYPES
                for uid in probe.expected_uids
                if uid in pages_by_uid
            ):
                ids.add(probe.id)
    return frozenset(ids)


def _report_only_probe_classes(scales: Iterable[str]) -> frozenset[str]:
    """Derive the set of probe classes carrying ``report_only: True`` at
    any of *scales*, straight from the real corpus (issue athenaeum#1776,
    athenaeum#1791 §2.1) -- the same "unit tests supply an explicit set,
    real callers reach the corpus here" escape hatch as
    :func:`_relationship_probe_ids`. :func:`compute_verdicts` excludes
    these classes from condition 2 (and condition 3) entirely, as if their
    rows were never in the store, so a new probe class can never move the
    ratified §7 kill criterion until an operator ruling on athenaeum#1736's
    thread promotes it (by editing ``tests.evals.corpus.CONDITION_2_ENROLLED``
    -- see that constant's docstring)."""
    classes: set[str] = set()
    for scale in scales:
        corpus = _corpus_for_scale(scale)
        for probe in corpus.probes:
            if probe.report_only:
                classes.add(probe.probe_class)
    return frozenset(classes)


#: Rank used to pick the WORST reading among probe classes present at a
#: scale for condition 3 (design doc §8: no aggregate score, but a single
#: per-scale PASS/FAIL still needs one reading named -- the worst one, so
#: a strong class can never paper over a failing one). ``"native-zero"``
#: (ruling R3) ranks alongside ``"limit"`` -- both are passing readings, so
#: neither can demote a genuine ``"fail"``/``"undefined"`` elsewhere, and
#: which of the two prints as "worst" among passing classes is immaterial
#: to the pass/fail outcome.
_READING_RANK = {
    "aspirational": 0,
    "target": 1,
    "limit": 2,
    "native-zero": 2,
    "fail": 3,
    "undefined": 4,
}
#: Readings that satisfy condition 3 -- ruling R3 adds ``"native-zero"`` to
#: the original three (design doc §7's ``<=2.0x``/``<=1.0x``/``<=0.5x``).
_CONDITION3_PASSING_READINGS = frozenset({"aspirational", "target", "limit", "native-zero"})


@dataclasses.dataclass(frozen=True)
class ScaleVerdict:
    """The three design-doc §7 conditions, evaluated at one corpus scale,
    all three against the SAME pinned ``verdict_arm`` (ruling R1).
    ``all_pass`` is what :func:`compute_cutoff_scale` walks.
    """

    corpus_scale: str
    verdict_arm: str
    condition1_pass: bool
    condition1_detail: str
    condition2_pass: bool
    condition2_detail: str
    condition3_pass: bool
    condition3_reading: str
    condition3_detail: str

    @property
    def all_pass(self) -> bool:
        return self.condition1_pass and self.condition2_pass and self.condition3_pass


def compute_verdicts(
    rows: Sequence[RolloutRow],
    *,
    verdict_arm: str = DEFAULT_VERDICT_ARM,
    relationship_probe_ids: frozenset[str] | None = None,
    report_only_classes: frozenset[str] | None = None,
    write_costs: Sequence[WriteCost] = (),
    probe_counts: Mapping[str, int] | None = None,
) -> list[ScaleVerdict]:
    """Compute :class:`ScaleVerdict` for every corpus scale present in
    *rows* (design doc §7).

    Ruling R1 (Quine review of PR#1740): every condition below reads the
    SAME *verdict_arm* on the Athenaeum side -- the shipped configuration,
    ``push_breadcrumb_pull`` by default. Never a different, most-favourable
    delivery arm per condition; that is the cherry-picking the review
    caught (condition 1/2 picking the most-correct arm, condition 3 picking
    the cheapest). Other Athenaeum arms still appear in every per-dimension
    table elsewhere in the report -- just never in these verdicts.

    *relationship_probe_ids* -- pass an explicit set (as every unit test in
    ``tests/evals/test_north_star_verdicts.py`` does) to avoid building a
    real corpus at all; ``None`` derives it via
    :func:`_relationship_probe_ids` from the scales actually present in
    *rows*.

    *report_only_classes* (issue athenaeum#1776): probe classes to exclude
    from conditions 2 and 3, as if their rows were never in *rows* at all --
    condition 1's relationship subset is unaffected (derived from class plus
    page type, never report-only status). Same escape hatch as
    *relationship_probe_ids*: ``None`` derives it via
    :func:`_report_only_probe_classes` from the real corpus; unit tests pass
    an explicit set.

    - **Condition 1** ("win the relationship use case", design §7.1,
      ruling R2): ONE POOLED correctness rate (correct/total) for
      *verdict_arm* over EVERY relationship-subset row at this scale,
      regardless of probe class -- never a max taken over per-class rates.
      Compared against the better of the two native arms' OWN pooled rate
      over the identical row set. Passes when *verdict_arm*'s pooled rate
      strictly exceeds it.
    - **Condition 2** ("do not lose any other use case", design §7.2,
      ruling R4): evaluated on the COMPLEMENT of the relationship subset
      (every row whose probe id is not in *relationship_probe_ids*),
      grouped by probe class -- abstention, distractor_robustness,
      redundancy, and any single_hop/multi_hop/disambiguation/temporal
      probe that did NOT make the relationship subset. For each such class
      present at the scale, *verdict_arm*'s correctness rate is compared
      against the better native arm's; a class where either side has no
      gradable rows is SKIPPED (counted, never silently dropped) rather
      than treated as either a pass or a failure. Fails if *verdict_arm* is
      strictly worse than native in any compared class; the detail always
      states how many classes were compared and how many were skipped
      (and their names), never a bare "not worse than native" that hides a
      skip.
    - **Condition 3** ("cost within budget", design §7.3, rulings R3/R5):
      the WORST reading (per :data:`_READING_RANK`) among
      :func:`compute_cost_ratios` entries at this scale (already computed
      against the same *verdict_arm*) that have native cost data at all --
      a probe class with NO native rows in it (``CostRatio.native_present``
      is ``False``) is SKIPPED and named (ruling R5), exactly the treatment
      condition 2 gives a one-sided class under R4, never a scale-level
      fail on its own. ``"undefined"`` and ``"fail"`` both fail the
      condition; ``"limit"``/``"target"``/``"aspirational"``/
      ``"native-zero"`` all pass. The scale fails condition 3 outright only
      when NO class at this scale has any native cost data to compare --
      there is nothing to certify a pass against.
    """
    # Issue athenaeum#1825: §7 verdicts are re-pinned to the "vector" arm
    # (operator ruling on issue athenaeum#1736, 2026-09-18 -- see
    # docs/design/native-memory-baseline.md Section 7 for the measured
    # basis: hybrid run 35315167602, medium pooled 27/33 vs grep 23/33, all
    # cost classes <= 2.0x). This SUPERSEDES the earlier fts5 pin (ruling
    # R1, issue athenaeum#1787) -- an fts5-backend row (and a
    # pre-athenaeum#1764 row whose `search_backend` is `None`, since the
    # field did not exist yet and every row that old was fts5-only) must
    # never enter this computation, even if a caller passes a mixed *rows*
    # sequence (the CLI's own check_floor_mismatch refuses to mix backends
    # into one --store, but this function has no such guarantee about its
    # caller). Only a REAL recorded `"vector"` row is kept.
    rows = [r for r in rows if r.record.search_backend == "vector"]
    if relationship_probe_ids is None:
        relationship_probe_ids = _relationship_probe_ids(
            {row.record.corpus_scale for row in rows}
        )
    if report_only_classes is None:
        report_only_classes = _report_only_probe_classes(
            {row.record.corpus_scale for row in rows}
        )

    # Condition 1's relationship subset is drawn from the UNFILTERED rows --
    # report_only status never touches it (it is derived from class plus
    # page type, not report_only). Conditions 2 and 3 both exclude
    # report_only classes from their row set entirely -- as if those rows
    # were never in the store -- so a new probe class cannot move the
    # ratified kill criterion (issue athenaeum#1776).
    relationship_rows = [r for r in rows if r.record.probe_id in relationship_probe_ids]
    non_report_only_rows = [r for r in rows if r.record.probe_class not in report_only_classes]
    other_rows = [
        r for r in non_report_only_rows if r.record.probe_id not in relationship_probe_ids
    ]

    other_stats = compute_group_stats(other_rows)
    ratios = compute_cost_ratios(
        compute_cost_per_correct(
            non_report_only_rows, write_costs=write_costs, probe_counts=probe_counts
        ),
        verdict_arm=verdict_arm,
    )

    scales = sorted(
        {r.record.corpus_scale for r in relationship_rows}
        | {s.corpus_scale for s in other_stats}
        | {r.corpus_scale for r in ratios}
    )

    verdicts: list[ScaleVerdict] = []
    for scale in scales:
        # Condition 1 (R2): ONE pooled rate over the whole relationship
        # subset at this scale, verdict_arm vs. the better native arm's OWN
        # pooled rate over the identical rows -- never a max over classes.
        scale_relationship_rows = [
            r for r in relationship_rows if r.record.corpus_scale == scale
        ]
        verdict_correct, verdict_total = _pooled_correctness(scale_relationship_rows, verdict_arm)
        verdict_rate = verdict_correct / verdict_total if verdict_total else None

        native_pooled: dict[str, float] = {}
        for native_arm in sorted(_NATIVE_ARM_VALUES):
            n_correct, n_total = _pooled_correctness(scale_relationship_rows, native_arm)
            if n_total:
                native_pooled[native_arm] = n_correct / n_total
        best_native_arm = (
            max(native_pooled, key=lambda arm: native_pooled[arm]) if native_pooled else None
        )
        best_native_rate = native_pooled.get(best_native_arm) if best_native_arm else None

        if verdict_rate is None or best_native_rate is None:
            condition1_pass = False
            condition1_detail = (
                f"condition 1: no gradable relationship-use-case rows for {verdict_arm!r} or "
                "the native arms at this scale"
            )
        elif verdict_rate > best_native_rate:
            condition1_pass = True
            condition1_detail = (
                f"condition 1: relationship use case won -- {verdict_arm!r} "
                f"{verdict_correct}/{verdict_total}={verdict_rate:.3f} > best native "
                f"({best_native_arm!r}) {best_native_rate:.3f}"
            )
        else:
            condition1_pass = False
            condition1_detail = (
                f"condition 1: relationship use case not won -- {verdict_arm!r} "
                f"{verdict_correct}/{verdict_total}={verdict_rate:.3f} <= best native "
                f"({best_native_arm!r}) {best_native_rate:.3f}"
            )

        # Condition 2 (R4): every OTHER probe class present, verdict_arm not
        # worse than the better native arm; skips are counted and named.
        other_by_class: dict[str, list[GroupStats]] = defaultdict(list)
        for s in other_stats:
            if s.corpus_scale == scale:
                other_by_class[s.probe_class].append(s)
        condition2_pass = True
        first_failure: str | None = None
        compared_classes: list[str] = []
        skipped_classes: list[str] = []
        for probe_class, class_group in sorted(other_by_class.items()):
            verdict_class_rates = [
                s.correctness_rate
                for s in class_group
                if s.arm == verdict_arm and s.correctness_rate is not None
            ]
            native_class_rates = [
                s.correctness_rate
                for s in class_group
                if s.arm in _NATIVE_ARM_VALUES and s.correctness_rate is not None
            ]
            if not verdict_class_rates or not native_class_rates:
                skipped_classes.append(probe_class)
                continue
            compared_classes.append(probe_class)
            verdict_class_rate = verdict_class_rates[0]
            best_native_class_rate = max(native_class_rates)
            if verdict_class_rate < best_native_class_rate and first_failure is None:
                condition2_pass = False
                first_failure = (
                    f"worse than native on {probe_class!r} "
                    f"({verdict_class_rate:.3f} < {best_native_class_rate:.3f})"
                )

        condition2_detail = (
            f"condition 2: compared {len(compared_classes)} classes, "
            f"skipped {len(skipped_classes)}"
        )
        if skipped_classes:
            condition2_detail += f" ({', '.join(skipped_classes)})"
        condition2_detail += "; " + (first_failure or "not worse than native on any compared class")

        # Condition 3 (R3 + R5): worst cost-ratio reading among CLASSES
        # WITH NATIVE DATA, already computed against verdict_arm by
        # compute_cost_ratios. Ruling R5: a class with no native rows at
        # all (ratio.native_present is False) is SKIPPED and named -- the
        # same treatment condition 2 gives a one-sided class under R4 --
        # never a scale-level fail on its own. The scale fails "undefined"
        # only when NO class at this scale had any native data to compare.
        ratios_at_scale = [r for r in ratios if r.corpus_scale == scale]
        cost_compared = [r for r in ratios_at_scale if r.native_present]
        cost_skipped = [r for r in ratios_at_scale if not r.native_present]
        cost_summary = (
            f"cost compared {len(cost_compared)} classes, skipped {len(cost_skipped)}"
        )
        if cost_skipped:
            cost_summary += f" ({', '.join(r.probe_class for r in cost_skipped)})"
        if not cost_compared:
            condition3_pass = False
            condition3_reading = "undefined"
            condition3_detail = (
                f"condition 3: {cost_summary}; no class at this scale has defined native cost"
            )
        else:
            worst = max(cost_compared, key=lambda r: _READING_RANK[r.reading])
            condition3_reading = worst.reading
            condition3_pass = worst.reading in _CONDITION3_PASSING_READINGS
            if worst.reading in ("undefined", "native-zero"):
                worst_detail = f"{worst.reading} for {worst.probe_class!r} ({worst.detail})"
            else:
                worst_detail = (
                    f"worst reading is {worst.reading!r} for {worst.probe_class!r} "
                    f"(ratio={worst.ratio:.3f})"
                )
            condition3_detail = f"condition 3: {cost_summary}; {worst_detail}"

        verdicts.append(
            ScaleVerdict(
                corpus_scale=scale,
                verdict_arm=verdict_arm,
                condition1_pass=condition1_pass,
                condition1_detail=condition1_detail,
                condition2_pass=condition2_pass,
                condition2_detail=condition2_detail,
                condition3_pass=condition3_pass,
                condition3_reading=condition3_reading,
                condition3_detail=condition3_detail,
            )
        )
    return verdicts


def compute_cutoff_scale(verdicts: Sequence[ScaleVerdict]) -> str:
    """The smallest scale, at or above ``"medium"``
    (:data:`_CUTOFF_ELIGIBLE_SCALES`), where all three conditions pass --
    design doc §7's "the number that goes into the README". ``"none"``
    when no eligible scale passes every condition; the caller (
    :func:`render_decision_block`) is what names the failing condition per
    scale, not this function.
    """
    by_scale = {v.corpus_scale: v for v in verdicts}
    for scale in _CUTOFF_ELIGIBLE_SCALES:
        verdict = by_scale.get(scale)
        if verdict is not None and verdict.all_pass:
            return scale
    return "none"


def render_cost_per_correct_table(costs: Sequence[CostPerCorrect]) -> list[str]:
    """Render *costs* as a markdown table. Phase 2 (write-cost) columns are
    included only when at least one row carries write-cost data -- a
    Phase-1-only store renders without them (issue athenaeum#1734 AC3)."""
    lines = ["## Cost per correct answer (athenaeum#1734)", ""]
    lines.append(
        "`cost_per_correct = (read_input_tokens + read_output_tokens + "
        "write_tokens_amortized) / correct_n` -- read cost only in Phase 1; Phase 2 adds "
        "write cost amortised over the full probe set at that scale (design doc §6), with "
        "the raw (unamortised) write spend printed alongside. `undefined` (never `inf`/`0`) "
        "means zero correct answers in the cell -- there is no denominator."
    )
    lines.append("")
    has_write = any(c.write_tokens_raw is not None for c in costs)
    header = "| probe_class | corpus_scale | arm | n | correct_n | read_tokens |"
    sep = "| --- | --- | --- | --- | --- | --- |"
    if has_write:
        header += " write_tokens_amortized | write_tokens_raw |"
        sep += " --- | --- |"
    header += " cost_per_correct |"
    sep += " --- |"
    lines.append(header)
    lines.append(sep)
    for c in sorted(costs, key=lambda c: (c.probe_class, c.corpus_scale, c.arm)):
        read_tokens = c.read_input_tokens + c.read_output_tokens
        row = (
            f"| {c.probe_class} | {c.corpus_scale} | {c.arm} | {c.n} | {c.correct_n} | "
            f"{read_tokens} |"
        )
        if has_write:
            amortized = (
                "n/a" if c.write_tokens_amortized is None else f"{c.write_tokens_amortized:.1f}"
            )
            raw = "n/a" if c.write_tokens_raw is None else str(c.write_tokens_raw)
            row += f" {amortized} | {raw} |"
        cost = "undefined" if c.cost_per_correct is None else f"{c.cost_per_correct:.1f}"
        row += f" {cost} |"
        lines.append(row)
    lines.append("")
    return lines


def _partial_banner_lines(report: NorthStarReport) -> list[str]:
    """The ``partial: N of M cells`` banner, or nothing (issue athenaeum#1751).

    Rendered at the very top of the decision block -- inside
    :func:`render_decision_block` rather than :func:`render_report`, so the
    warning travels with the block for every caller that renders it
    directly, and so the first thing a reader of the go/no-go answer sees
    is whether the answer rests on the whole grid.

    Distinct from the ``PARTIAL RUN`` banner :func:`render_report` prints
    for ``report.aborted``: that one says the run RAISED; this one says the
    store holds fewer rows than the run planned, which is also what a job
    killed at its ``timeout-minutes`` leaves behind -- a case where nothing
    ever raised because the process was never given the chance.
    """
    planned = report.planned_cells
    torn = report.torn_rows
    # DISTINCT cell keys, never ``len(report.rows)``: a resumed run re-runs
    # a partly-completed group in full, so the store legitimately holds more
    # rows than cells. Counting rows could exceed ``planned`` and hide a
    # genuinely partial run behind an apparently-complete one. The loader
    # already de-duplicates, but computing it here means the banner is right
    # for any caller, including one that assembled rows itself.
    completed = len({row.cell.cell_key() for row in report.rows})
    if planned is None or completed >= planned:
        # Complete (or unknowable). A torn row is still worth saying out
        # loud even then -- it means a cell was paid for and its result is
        # unreadable, which is not something to leave only in a log.
        if torn:
            return [f"> **{_torn_phrase(torn)} ignored** — unreadable store rows.", ""]
        return []

    banner = (
        f"> **partial: {completed} of {planned} cells** — the grid did not "
        "finish, so every figure below is computed over the cells that did. Read "
        "the verdicts as provisional."
    )
    if torn:
        banner += f" {_torn_phrase(torn).capitalize()} ignored (unreadable store rows)."
    if report.duplicate_rows:
        banner += (
            f" {report.duplicate_rows} superseded row(s) from a resumed group "
            "collapsed to their latest."
        )
    return [banner, ""]


def _torn_phrase(torn: int) -> str:
    return f"{torn} torn row" if torn == 1 else f"{torn} torn rows"


def render_decision_block(
    report: NorthStarReport,
    verdicts: Sequence[ScaleVerdict] | None = None,
    *,
    verdict_arm: str | None = None,
) -> list[str]:
    """The decision block (issue athenaeum#1734 AC5): rendered at the TOP
    of the report, before any table -- the reader gets the go/no-go answer
    first, the supporting dimension breakdowns after. *verdicts* lets a
    caller (or a test) supply pre-computed verdicts; ``None`` computes them
    from ``report.rows``/``report.write_costs``/``report.verdict_arm``
    (or *verdict_arm*, if given -- it overrides ``report.verdict_arm`` for
    this render only).
    """
    resolved_verdict_arm = verdict_arm if verdict_arm is not None else report.verdict_arm
    if verdicts is None:
        # Issue athenaeum#1819: graded_rows excludes harness-failed cells
        # (never graded as an ordinary miss).
        verdicts = compute_verdicts(
            report.graded_rows, verdict_arm=resolved_verdict_arm, write_costs=report.write_costs
        )
    cutoff = compute_cutoff_scale(verdicts)

    lines: list[str] = []
    lines.extend(_partial_banner_lines(report))
    lines.append("## Decision (design doc §7, athenaeum#1734)")
    lines.append("")
    lines.append(
        f"**Verdict arm:** `{resolved_verdict_arm}` (ruling R1) -- the shipped configuration "
        "is the ONE Athenaeum arm every condition below reads. Other Athenaeum arms still "
        "appear in the per-dimension tables further down, but never in these verdicts -- no "
        "picking the most-correct arm for conditions 1-2 and the cheapest for condition 3."
    )
    lines.append("")
    lines.append(
        "Three conditions, evaluated per scale, all against the verdict arm above: **(1)** "
        "win the relationship use case (single_hop/multi_hop/disambiguation/temporal probes "
        "targeting person/company pages), one POOLED correctness rate over the whole subset, "
        "against the better native arm's own pooled rate on the same rows; **(2)** do not "
        "lose any other current use case, class by class, skipping (and naming) any class "
        "where either side has no gradable rows; **(3)** cost per correct answer within "
        "budget of the better native arm, per class, skipping (and naming) any class with no "
        "native cost data at all (ruling R5) -- `>2.0x fail`, `<=2.0x limit`, `<=1.0x target`, "
        "`<=0.5x aspirational`, or `native-zero` (a pass) when the better native arm scored "
        "zero correct answers while the verdict arm scored at least one."
    )
    lines.append("")
    report_only_classes = _report_only_probe_classes(
        {row.record.corpus_scale for row in report.rows}
    )
    lines.append(
        "report-only classes excluded: "
        + (", ".join(sorted(report_only_classes)) if report_only_classes else "(none)")
        + " -- excluded from conditions 2 and 3 entirely (issue athenaeum#1776); promotion is "
        "an explicit operator ruling on athenaeum#1736's thread, recorded by editing "
        "`tests.evals.corpus.CONDITION_2_ENROLLED`, never a side effect of this report."
    )
    lines.append("")
    lines.append(
        "**\"Better native arm\"** means two different things across these conditions, both "
        "the harshest reading available to Athenaeum: for condition 1, the native arm with "
        "the HIGHER pooled correctness rate; for condition 3, the native arm with the CHEAPER "
        "DEFINED cost per correct (a native arm that scored zero correct answers has no "
        "defined cost and is never picked as \"cheaper\" by that alone -- see `native-zero`)."
    )
    lines.append("")

    if report.write_costs:
        lines.append(
            "Phase 2: write cost is amortised over the full probe set at each scale it "
            "applies to --"
        )
        for scale in sorted({wc.corpus_scale for wc in report.write_costs}):
            probe_count = len(_corpus_for_scale(scale).probes)
            lines.append(f"- write cost amortised over {probe_count} probes at `{scale}`.")
        lines.append("")

    if cutoff == "none":
        failing_lines = []
        for verdict in verdicts:
            if verdict.all_pass:
                continue
            failing = [
                detail
                for passed, detail in (
                    (verdict.condition1_pass, verdict.condition1_detail),
                    (verdict.condition2_pass, verdict.condition2_detail),
                    (verdict.condition3_pass, verdict.condition3_detail),
                )
                if not passed
            ]
            failing_lines.append(f"- `{verdict.corpus_scale}`: " + "; ".join(failing))

        if failing_lines:
            lines.append(
                "**Cutoff scale:** `none` -- no scale at or above `medium` passed all three "
                "conditions. Failing condition per scale:"
            )
            lines.append("")
            lines.extend(failing_lines)
            lines.append("")
        elif not verdicts and report.graded_rows and not any(
            row.record.search_backend == "vector" for row in report.graded_rows
        ):
            # Issue athenaeum#1825: the §7 arm is pinned to `search_backend
            # == "vector"` by operator ruling on issue athenaeum#1736
            # (2026-09-18) -- an fts5-only store (every row pre-athenaeum#1825,
            # or an explicit fts5 dispatch) has rows, but NONE that
            # `compute_verdicts` will admit, so `verdicts` comes back empty
            # even though the store is not itself empty. Distinguished from
            # the genuinely-empty-store branch below so an operator reading
            # this report is told WHY, not left to guess between "no run
            # happened yet" and "the wrong backend was dispatched".
            lines.append(
                "**Cutoff scale:** `none` -- this store has rows, but none recorded "
                "`search_backend=\"vector\"`. The §7 verdict arm is pinned to `vector` by "
                "operator ruling on issue athenaeum#1736 (2026-09-18); an fts5-only store "
                "has nothing this decision block can evaluate. Dispatch a `vector` "
                "north-star run to populate it."
            )
            lines.append("")
        else:
            lines.append(
                "**Cutoff scale:** `none` -- no scale at or above `medium` was evaluated in "
                "this run (see the eligibility column below), so there is nothing to certify "
                "a cutoff against."
            )
            lines.append("")
    else:
        lines.append(
            f"**Cutoff scale:** `{cutoff}` -- the smallest scale at or above `medium` where "
            "all three conditions hold. Below it the recommendation is the agent's own memory."
        )
        lines.append("")

    lines.append(
        "| scale | cutoff eligible | condition 1 | condition 2 | condition 3 | reading | "
        "all pass |"
    )
    lines.append("| --- | --- | --- | --- | --- | --- | --- |")
    for verdict in verdicts:
        eligible = "yes" if verdict.corpus_scale in _CUTOFF_ELIGIBLE_SCALES else "no"
        lines.append(
            f"| {verdict.corpus_scale} | {eligible} | "
            f"{'pass' if verdict.condition1_pass else 'fail'} | "
            f"{'pass' if verdict.condition2_pass else 'fail'} | "
            f"{'pass' if verdict.condition3_pass else 'fail'} | "
            f"{verdict.condition3_reading} | "
            f"{'pass' if verdict.all_pass else 'fail'} |"
        )
    lines.append("")
    return lines


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
    # issue athenaeum#1734: Phase 2 write SPEND (distinct from write_path_stats'
    # retention accuracy), empty for a Phase-1-only run. Appended last so
    # existing positional construction and sibling-lane merges stay safe.
    write_costs: tuple[WriteCost, ...] = ()
    # issue athenaeum#1830 AC2: the "filing loss" row, separate from
    # write_path_stats -- empty for a Phase-1-only run and for any
    # write_path_stats row that carries no matching filing-loss measurement
    # (e.g. compile_observation_stream's diagnostic path, which has no
    # native memory files to file).
    filing_loss_stats: tuple[FilingLossStats, ...] = ()
    # ruling R1 (Quine review of PR#1740): the ONE Athenaeum arm every §7
    # verdict reads. Stored on the report (not just passed as a function
    # arg) so render_report/render_decision_block always render the SAME
    # arm the report was built with, without a caller having to thread it
    # through separately.
    verdict_arm: str = DEFAULT_VERDICT_ARM
    # issue athenaeum#1819: `rows` above MINUS every row whose
    # RolloutRecord.harness_failure is non-null (a permission-request or
    # empty-breadcrumb cell) -- the subset compute_group_stats/weak_probes/
    # compute_verdicts/compute_cost_per_correct actually run against, so a
    # harness failure is never graded as an ordinary miss. `rows` itself
    # keeps every cell, harness failures included, for the header's "total
    # rollout rows" count and the mode-per-cell table. Equal to `rows` for
    # every pre-athenaeum#1819 report (nothing was ever checked for this,
    # so nothing is excluded) and for any run with zero harness failures.
    graded_rows: tuple[RolloutRow, ...] = ()
    # issue athenaeum#1819: len(rows) - len(graded_rows), i.e. how many
    # cells this report excluded from correctness/cost grading. Printed
    # unconditionally in the header so a reader never has to diff the two
    # row counts by hand.
    harness_failure_count: int = 0
    # issue athenaeum#1751: how many cells the run INTENDED to complete, read
    # from the store's planned-count sidecar. ``None`` means "unknowable"
    # (a store written before that sidecar existed, or one whose sidecar was
    # lost) and renders no partial banner -- absence of evidence is not
    # evidence of a partial run. Appended last, per the convention the
    # ``write_costs`` comment above states.
    planned_cells: int | None = None
    # issue athenaeum#1751: store lines that would not decode -- a process
    # killed mid-``write`` leaves a partial row, and a rollout row is
    # hundreds of KB. Surfaced in the banner so a paid-for-but-unreadable
    # cell is visible rather than merely survived.
    torn_rows: int = 0
    # issue athenaeum#1751: rows superseded by a later row for the same cell,
    # which a resumed group produces by design (see
    # ``load_rollout_rows_and_diagnostics``). Reported alongside the torn
    # count so a reader can tell "this store was resumed" from "this store
    # is damaged".
    duplicate_rows: int = 0
    # issue athenaeum#1761: the ONE relevance-floor value active across every
    # row in this report, or ``None`` when no row carries a floor (a
    # floor-off run, or a store written before this field existed -- both
    # decode as ``None`` on ``RolloutRecord.relevance_floor_vector``/
    # ``.relevance_floor_fts5``, indistinguishable and correctly so: both
    # ARE "no floor was active"). :func:`build_report` refuses to construct
    # a report at all when *rows* carry more than one distinct value --
    # see that function's own docstring.
    relevance_floor_vector: float | None = None
    relevance_floor_fts5: float | None = None
    # issue athenaeum#1785: one human-readable summary line describing this
    # run's Phase 2 write-path configuration (on/off, --phase2-scales,
    # --phase2-systems, and an API-writer fidelity marker when the native
    # system ran in api mode) -- computed by north_star_cli.py, which owns
    # the dispatch flags this describes, and passed straight through here
    # for the header to render. Empty string (the default) renders nothing,
    # byte-identical to a pre-athenaeum#1785 report.
    phase2_summary: str = ""
    # issue athenaeum#1785 (Quine review of PR#1813, should-fix 1): the
    # (system, corpus_scale) pairs whose write_path_stats/write_costs rows
    # came from a PARTIAL (exit 75) athenaeum compile -- read from the
    # sibling store's ``meta`` rows, which this dataclass otherwise never
    # sees (WritePathStats/WriteCost carry no partial flag of their own).
    # render_report marks each matching row in the "Write path (Phase 2)"
    # table rather than rendering it identically to a clean compile, so a
    # reader never mistakes a deadline-tripped run's numbers for a
    # completed one. Empty frozenset (the default) marks nothing.
    phase2_partial: frozenset[tuple[str, str]] = frozenset()
    # issue athenaeum#1836: per (arm, corpus_scale) counts of rows whose
    # RolloutRecord.turns_exhausted is True -- a SUBSET of harness_failure_
    # count (every turn_cap row is also a harness failure, via the
    # "turn_cap" reason rollout.py stamps), broken out per arm/scale so a
    # cap that bites one arm hard is visible instead of drowning in the
    # single global harness_failure_count total. Computed over `rows`
    # (every row, not just graded_rows -- a turn_cap row is BY DEFINITION
    # excluded from graded_rows). Empty tuple (the default) for every
    # pre-athenaeum#1836 report and for any run with zero turn-cap rows.
    turn_cap_counts: tuple[TurnCapCount, ...] = ()


class MixedFloorError(ValueError):
    """Raised by :func:`_pooled_floor_value` when *rows* carry more than one
    distinct floor value for one attribute (issue athenaeum#1761).

    A ``ValueError`` subclass, not a bare one (issue athenaeum#1764):
    ``north_star_cli.main`` needs to catch EXACTLY this failure mode to run
    its PARTIAL-report recovery path -- ``build_report`` also reaches
    ``_corpus_for_scale``/``build_corpus``, which raises a plain
    ``ValueError`` for an unknown corpus scale, and that is a different
    failure the recovery path must NOT swallow (retrying with
    ``pool_floor_values=False`` would hit the exact same unknown-scale
    ``ValueError`` again, uncaught, the second time). Subclassing
    ``ValueError`` rather than replacing it also keeps every existing
    ``pytest.raises(ValueError, ...)`` assertion in
    ``test_relevance_floor_input.py`` passing unchanged.
    """


def _pooled_floor_value(rows: Sequence[RolloutRow], attr: str) -> float | None:
    """The ONE value *attr* (``"relevance_floor_vector"`` or
    ``"relevance_floor_fts5"``) takes across every row's
    :class:`~tests.evals.rollout.RolloutRecord`, or raise (issue
    athenaeum#1761).

    A report is one decision block over one comparable run. Pooling rows
    from a floor-off pass with rows from a floor-on pass (or two
    differently-configured floor-on passes) into the same correctness/cost
    figures would silently misattribute one pass's numbers to the other's
    configuration -- there is no safe default reading here, so this raises
    rather than picking a value or dropping rows.
    """
    values = {getattr(row.record, attr) for row in rows}
    if len(values) > 1:
        distinct = sorted(
            values, key=lambda v: (v is None, v if v is not None else 0.0)
        )
        raise MixedFloorError(
            f"north_star_report.build_report: rows carry differing {attr} values "
            f"{distinct!r} -- refusing to pool a floor-on run together with a "
            "floor-off run (or two differently-configured floor-on runs) into one "
            "decision block (issue athenaeum#1761). Dispatch a floor-on grid to its "
            "own --store path and build a report from that store alone."
        )
    return next(iter(values), None)


@dataclasses.dataclass(frozen=True)
class TurnCapCount:
    """One (arm, corpus_scale) group's count of rows whose
    ``RolloutRecord.turns_exhausted`` is ``True`` (issue athenaeum#1836) --
    the per-group breakdown of ``NorthStarReport.harness_failure_count``'s
    ``"turn_cap"`` slice, rendered next to that single global count so a
    cap that bites one arm/scale hard is visible."""

    arm: str
    corpus_scale: str
    count: int


def _turn_cap_counts(rows: Sequence[RolloutRow]) -> tuple[TurnCapCount, ...]:
    """:class:`TurnCapCount` for every (arm, corpus_scale) group in *rows*
    with at least one ``turns_exhausted`` row, sorted for a stable report
    ordering. Computed over ALL of *rows*, never ``graded_rows`` -- a
    turn_cap row is by definition excluded from ``graded_rows``, so
    counting only over ``graded_rows`` would always read zero."""
    counts: dict[tuple[str, str], int] = defaultdict(int)
    for row in rows:
        if row.record.turns_exhausted:
            counts[(row.record.arm.value, row.record.corpus_scale)] += 1
    return tuple(
        TurnCapCount(arm=arm, corpus_scale=corpus_scale, count=count)
        for (arm, corpus_scale), count in sorted(counts.items())
    )


def build_report(
    rows: Sequence[RolloutRow],
    *,
    aborted: bool = False,
    abort_reason: str = "",
    write_path_stats: Sequence[WritePathStats] = (),
    write_costs: Sequence[WriteCost] = (),
    filing_loss_stats: Sequence[FilingLossStats] = (),
    verdict_arm: str = DEFAULT_VERDICT_ARM,
    planned_cells: int | None = None,
    torn_rows: int = 0,
    duplicate_rows: int = 0,
    pool_floor_values: bool = True,
    phase2_summary: str = "",
    phase2_partial: Sequence[tuple[str, str]] = (),
) -> NorthStarReport:
    """Assemble a :class:`NorthStarReport` from decoded result-store rows.

    Pure computation -- constructs no model client and makes no network
    call (see the module docstring's "No LLM judge" note). *write_path_stats*
    is an independent, pre-computed input (issue athenaeum#1726): unlike
    *rows*, it cannot be derived from a :class:`ResultStore` here, since it
    is scored against an :class:`~tests.evals.corpus.Observation` stream and
    a system's raw store contents, neither of which a ``RolloutRow`` carries.
    *filing_loss_stats* is the same kind of pre-computed input, one level
    further down the pipeline (issue athenaeum#1830 AC2): retention of the
    compiled store against the NATIVE writer's own retained tokens, not the
    full observation stream -- see :class:`FilingLossStats`.

    Raises :class:`ValueError` (issue athenaeum#1761) when *rows* carry more
    than one distinct ``relevance_floor_vector`` or ``relevance_floor_fts5``
    value -- see :func:`_pooled_floor_value`. A store with every row at
    ``None`` (no operator ever set a floor) is the pre-athenaeum#1761
    behaviour and passes through unchanged.

    *pool_floor_values* (issue athenaeum#1764) -- ``False`` skips
    :func:`_pooled_floor_value` entirely and reports both floor fields as
    ``None`` instead of raising. This exists ONLY for
    ``north_star_cli.main``'s abort-recovery path: when the normal ``True``
    call already raised once for a mixed-floor store, ``main`` needs a
    SECOND, non-raising call to still produce a PARTIAL report over the
    same rows (naming the mismatch in ``abort_reason`` instead) rather than
    leaving the run with no report at all. No other caller should pass
    ``False`` -- pooling is the correctness check this issue's predecessor
    (athenaeum#1761) added, and skipping it silently is exactly what that
    check exists to prevent.
    """
    if pool_floor_values:
        relevance_floor_vector = _pooled_floor_value(rows, "relevance_floor_vector")
        relevance_floor_fts5 = _pooled_floor_value(rows, "relevance_floor_fts5")
    else:
        relevance_floor_vector = None
        relevance_floor_fts5 = None
    scales = sorted({row.record.corpus_scale for row in rows})
    digests = {scale: _corpus_for_scale(scale).fingerprint() for scale in scales}
    # Issue athenaeum#1819: never grade a harness-failed cell (a
    # permission-request or empty-breadcrumb cli-mode row) as correctness/
    # cost data -- `graded_rows` is the input to every stat/verdict below;
    # `rows` itself is preserved in full on the report (see NorthStarReport.rows).
    graded_rows = [row for row in rows if not row.record.harness_failure]
    harness_failure_count = len(rows) - len(graded_rows)
    return NorthStarReport(
        rows=tuple(rows),
        graded_rows=tuple(graded_rows),
        harness_failure_count=harness_failure_count,
        stats=tuple(compute_group_stats(graded_rows)),
        aborted=aborted,
        abort_reason=abort_reason,
        athenaeum_version=_get_version(),
        git_sha=_get_git_sha(),
        generated=now_iso(),
        corpus_digests=digests,
        weak_probes=weak_probes(graded_rows),
        write_path_stats=tuple(write_path_stats),
        write_costs=tuple(write_costs),
        filing_loss_stats=tuple(filing_loss_stats),
        verdict_arm=verdict_arm,
        planned_cells=planned_cells,
        torn_rows=torn_rows,
        duplicate_rows=duplicate_rows,
        relevance_floor_vector=relevance_floor_vector,
        relevance_floor_fts5=relevance_floor_fts5,
        phase2_summary=phase2_summary,
        phase2_partial=frozenset(phase2_partial),
        turn_cap_counts=_turn_cap_counts(rows),
    )


def _fmt(value: float | None, digits: int = 3) -> str:
    return "n/a" if value is None else f"{value:.{digits}f}"


def _report_search_backend_display(rows: Sequence[RolloutRow]) -> str:
    """Issue athenaeum#1787: the distinct ``search_backend`` value(s)
    carried by *rows*, for the report header -- so a reader can tell a
    vector-dispatch report from the default fts5 report at a glance without
    opening the store JSONL. A ``None`` backend (pre-athenaeum#1764 store)
    reads as ``"fts5"``, the only backend that existed before that issue.
    ``check_floor_mismatch`` already refuses to mix backends into one
    ``--store`` at dispatch time, so this is normally a single value; a
    genuinely mixed set (only reachable via ``--allow-floor-mismatch``)
    prints every distinct value so the header never hides that a mismatch
    was allowed through.
    """
    backends = sorted({r.record.search_backend or "fts5" for r in rows})
    return ", ".join(backends) if backends else "fts5"


def _report_hybrid_display(rows: Sequence[RolloutRow]) -> str | None:
    """Issue athenaeum#1816: whether the vector backend's RRF hybrid fusion
    (``mcp_server.recall_search``'s ``backend_name == "vector" and
    resolve_recall_hybrid(config)`` block, DEFAULT ON) had a real FTS5
    index to fuse against, for the report header -- so a vector-dispatch
    report visibly distinguishes "measured the shipped hybrid" from "fell
    back to vector-only ranking for every call" (the exact defect this
    issue reports) without a reader opening the store JSONL.

    Returns ``None`` when *rows* carries no vector-backend row at all --
    the question does not apply to an fts5/keyword-only report, and the
    header omits the line entirely rather than printing a value for a
    backend that was never dispatched. ``"unknown"`` when every vector row
    predates the ``hybrid_active`` field (back-compat). ``"mixed"`` for a
    store somehow carrying both true and false (unreachable through normal
    dispatch -- one grid, one fix -- but never silently averaged away).
    Otherwise ``"on"``/``"off"`` for the single value every vector row in
    this store shares.
    """
    vector_rows = [r for r in rows if (r.record.search_backend or "fts5") == "vector"]
    if not vector_rows:
        return None
    known = {
        r.record.hybrid_active for r in vector_rows if r.record.hybrid_active is not None
    }
    if not known:
        return "unknown"
    if len(known) > 1:
        return "mixed"
    return "on" if next(iter(known)) else "off"


def _report_config_isolated_display(rows: Sequence[RolloutRow]) -> str | None:
    """Issue athenaeum#1819 defect 3: whether every cli-mode row in *rows*
    ran under an operator-isolated ``CLAUDE_CONFIG_DIR``, for the report
    header -- mirrors :func:`_report_hybrid_display`'s shape exactly.

    Returns ``None`` when *rows* carries no cli-mode row at all (an
    api-only report never spawns ``claude -p``, so the question does not
    apply and the header omits the line). ``"mixed"`` when some cli-mode
    rows are isolated and others are not (unreachable through normal
    dispatch now that :func:`tests.evals.rollout._require_isolated_cli_config`
    refuses an unset ``CLAUDE_CONFIG_DIR`` before any spawn, but never
    silently averaged away). Otherwise ``"yes"``/``"no"`` for the single
    value every cli-mode row in *rows* shares.
    """
    cli_rows = [r for r in rows if r.record.mode == "cli"]
    if not cli_rows:
        return None
    known = {r.record.config_isolated for r in cli_rows}
    if len(known) > 1:
        return "mixed"
    return "yes" if next(iter(known)) else "no"


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
    # issue athenaeum#1787: printed unconditionally so a vector-dispatch
    # report (a distinct --store, per check_floor_mismatch) is visibly
    # labelled its own section beside the default fts5 report, never
    # merged into it.
    lines.append(f"- search_backend: {_report_search_backend_display(report.rows)}")
    # issue athenaeum#1816: printed only for a store that actually carries a
    # vector row -- see _report_hybrid_display's own docstring.
    hybrid_display = _report_hybrid_display(report.rows)
    if hybrid_display is not None:
        lines.append(f"- hybrid: {hybrid_display}")
    # issue athenaeum#1761: printed unconditionally, "off" when no operator
    # ever set a floor -- so a reader never has to infer floor status from
    # absence.
    floor_vector_display = (
        "off" if report.relevance_floor_vector is None else str(report.relevance_floor_vector)
    )
    floor_fts5_display = (
        "off" if report.relevance_floor_fts5 is None else str(report.relevance_floor_fts5)
    )
    lines.append(f"- relevance_floor_vector: {floor_vector_display}")
    lines.append(f"- relevance_floor_fts5: {floor_fts5_display}")
    # issue athenaeum#1785: printed only when non-empty, so a pre-Phase-2
    # report (or a Phase-2-off run, which north_star_cli.py leaves this
    # blank for) renders byte-identical to before this field existed.
    if report.phase2_summary:
        lines.append(f"- phase2: {report.phase2_summary}")
    for scale in sorted(report.corpus_digests):
        lines.append(f"- corpus_digest[{scale}]: {report.corpus_digests[scale]}")
    # issue athenaeum#1842: Corpus.fingerprint() digests PAGES only (see
    # tests/evals/corpus.py's own note beside it), so a change to the GRADER
    # -- which is what this issue shipped -- moves every correctness figure
    # in this report while leaving corpus_digest byte-identical. Stamped
    # beside the digest, unconditionally, so two reports over the same store
    # are never silently assumed comparable: a reader comparing tables must
    # be able to see that the rule changed, not infer it from a number
    # moving.
    lines.append(f"- grader_revision: {GRADER_REVISION}")
    lines.append(f"- total rollout rows: {len(report.rows)}")
    # issue athenaeum#1819: printed unconditionally -- 0 is a real, useful
    # value (this run had none), not something to hide by omission.
    lines.append(f"- harness failures: {report.harness_failure_count}")
    # issue athenaeum#1836: per (arm, corpus_scale) turn_cap breakdown,
    # printed only when non-empty -- a run with zero turn-cap rows renders
    # byte-identical to a pre-athenaeum#1836 report.
    for tc in report.turn_cap_counts:
        lines.append(f"  - turn_cap[arm={tc.arm}, scale={tc.corpus_scale}]: {tc.count}")
    # issue athenaeum#1819 defect 3: printed only for a store that actually
    # carries a cli-mode row -- see _report_config_isolated_display's own
    # docstring.
    config_isolated_display = _report_config_isolated_display(report.rows)
    if config_isolated_display is not None:
        lines.append(f"- config isolated: {config_isolated_display}")
    lines.append("")
    lines.append(
        "No LLM judge is invoked anywhere on this path — every figure below is a "
        "deterministic computation over already-captured rollout transcripts. This is "
        "a **measurement, not a regression gate**; nothing here should ever fail a build."
    )
    lines.append("")

    lines.extend(render_decision_block(report))

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

    lines.append("## Mode per cell (athenaeum#1733)")
    lines.append("")
    lines.append(
        "Which execution path produced each rollout cell -- `api` (Anthropic Messages API "
        "tool-use loop, the primary path per `docs/design/native-memory-baseline.md` §4) or "
        "`cli` (`claude -p`, the fidelity spot-check). A direct render over `report.rows` "
        "itself, never folded into `GroupStats`/`compute_group_stats`."
    )
    lines.append("")
    lines.append("| probe_id | arm | corpus_scale | replicate | mode | tag_followed |")
    lines.append("| --- | --- | --- | --- | --- | --- |")
    for row in sorted(
        report.rows,
        key=lambda r: (
            r.record.probe_id,
            r.record.arm.value,
            r.record.corpus_scale,
            r.cell.replicate,
        ),
    ):
        # Issue athenaeum#1831: report-only per row, feeds no GroupStats
        # win/loss and no §7 condition -- see tag_followed's own docstring.
        row_probe = _probe_for_row(row)
        followed = tag_followed(row.record, row_probe)
        followed_display = "n/a" if followed is None else ("yes" if followed else "no")
        lines.append(
            f"| {row.record.probe_id} | {row.record.arm.value} | {row.record.corpus_scale} | "
            f"{row.cell.replicate} | {row.record.mode} | {followed_display} |"
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

    lines.extend(
        render_cost_per_correct_table(
            # Issue athenaeum#1819: graded_rows excludes harness-failed cells.
            compute_cost_per_correct(report.graded_rows, write_costs=report.write_costs)
        )
    )

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
        "tokens to grade against. Those tokens are the corpus pages' own internal "
        "reference tags, so every arm's system prompt carries one identical instruction "
        "(issue athenaeum#1753) to end the answer with `[ref: TAG]` for each page relied on, "
        "or `[ref: none]` for none — correctness therefore reads as “did the arm reach the "
        "right page and say so”. Rows recorded before that contract landed carry no tags "
        "and grade at or near 0 for every arm, including ORACLE."
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

    lines.append("## Harm (forbidden-token) rate (issue athenaeum#1772, report_only)")
    lines.append("")
    lines.append(
        "`harm_free_rate` grades whether the answer avoided every one of a probe's planted "
        "`forbidden_tokens` -- see `grade_harm`. Same normalizer and substring instrument as "
        "`correctness_rate`, no LLM judge. `report_only` (issue athenaeum#1791 §2.1): this "
        "mechanism feeds no §7 condition and `compute_verdicts` is unchanged by it. `n/a` "
        "means no probe in that group carries `forbidden_tokens` to grade against -- true for "
        "every probe class shipped so far, so this section reads `n/a` throughout the current "
        "corpus."
    )
    lines.append("")
    lines.append("| probe_class | corpus_scale | arm | n | harm_free_rate |")
    lines.append("| --- | --- | --- | --- | --- |")
    for s in report.stats:
        lines.append(
            f"| {s.probe_class} | {s.corpus_scale} | {s.arm} | {s.n} | "
            f"{_fmt(s.harm_free_rate)} |"
        )
    lines.append("")

    lines.append("## Coverage (fraction of planted tokens) (issue athenaeum#1773, report_only)")
    lines.append("")
    lines.append(
        "`coverage_rate` is the mean fraction of a probe's `answer_tokens` present in the "
        "answer -- see `grade_coverage`. Same normalizer and substring instrument as "
        "`correctness_rate`, but a ratio rather than an all-or-nothing match, so a "
        "many-correct-answer probe (the `aggregation` class) can grade partial credit instead "
        "of a forced zero. `report_only` (issue athenaeum#1791 §2.1): this mechanism feeds no "
        "§7 condition and `compute_verdicts` is unchanged by it. `n/a` means no probe in that "
        "group carries `answer_tokens` to grade against. For a single-`answer_tokens` probe, "
        "`coverage_rate` and `correctness_rate` are the same number by construction."
    )
    lines.append("")
    lines.append("| probe_class | corpus_scale | arm | n | coverage_rate |")
    lines.append("| --- | --- | --- | --- | --- |")
    for s in report.stats:
        lines.append(
            f"| {s.probe_class} | {s.corpus_scale} | {s.arm} | {s.n} | "
            f"{_fmt(s.coverage_rate)} |"
        )
    lines.append("")

    lines.append("## Marker resolution (issue athenaeum#1730, report_only)")
    lines.append("")
    lines.append(
        "`marker_resolution_rate` is the share of answers whose CITED `[^src-N]` footnote "
        "markers all resolve to a footnote definition on a page that plants one of the "
        "probe's `answer_tokens` -- see `grade_marker_resolution`. It asks whether an answer "
        "that quotes a citation quoted a real one, now that compiled pages carry inline "
        "per-claim markers and `recall` resolves them on the hit. `report_only`: this "
        "mechanism feeds no §7 condition and `compute_verdicts` is unchanged by it. `n/a` "
        "means no answer in that group cited a marker, or no token-bearing expected page "
        "defines one -- both are 'nothing to grade', never a graded zero."
    )
    lines.append("")
    lines.append("| probe_class | corpus_scale | arm | n | marker_resolution_rate |")
    lines.append("| --- | --- | --- | --- | --- |")
    for s in report.stats:
        lines.append(
            f"| {s.probe_class} | {s.corpus_scale} | {s.arm} | {s.n} | "
            f"{_fmt(s.marker_resolution_rate)} |"
        )
    lines.append("")

    lines.append("## Reference tag followed (issue athenaeum#1831, report_only)")
    lines.append("")
    lines.append(
        "`tag_followed_rate` is the share of answers that quoted the `[ref: TAG]` "
        "`REFERENCE_TAG_INSTRUCTION` asks for, for every one of the probe's planted tags -- "
        "see `tag_followed`. This is the athenaeum#1753 correctness rule, DEMOTED by the "
        "operator ruling on athenaeum#1791 comment 5732689494: it feeds no §7 condition and "
        "no `correctness_rate` above, which is now graded on `answer_markers` (the planted "
        "fact) plus delivered-page evidence, never this tag. `n/a` means no probe in that "
        "group carries `answer_tokens` to grade against (including every abstention group)."
    )
    lines.append("")
    lines.append("| probe_class | corpus_scale | arm | n | tag_followed_rate |")
    lines.append("| --- | --- | --- | --- | --- |")
    for s in report.stats:
        lines.append(
            f"| {s.probe_class} | {s.corpus_scale} | {s.arm} | {s.n} | "
            f"{_fmt(s.tag_followed_rate)} |"
        )
    lines.append("")

    lines.append("## Marker miss with delivery (issue athenaeum#1842, report_only)")
    lines.append("")
    lines.append(
        "`marker_miss_with_delivery` is the COUNT of gradable cells in that group where "
        "every answer-bearing page WAS delivered to the arm (`correctness_rate`'s "
        "delivered-page-evidence clause held) and at least one of those pages still "
        "contributed no matching `answer_markers` value to the answer -- see "
        "`marker_miss_with_delivery`. It splits a `False` cell into its two causes: a "
        "delivery gap (counted here as 0) versus a model miss or a marker-matching gap "
        "(counted here as 1). `report_only`: this column feeds no §7 condition, no "
        "`compute_verdicts` input and no win/loss field, exactly as `tag_followed_rate` "
        "above does not. `n/a` means no row in that group is gradable at all (including "
        "every abstention group) -- never a counted zero."
    )
    lines.append("")
    lines.append("| probe_class | corpus_scale | arm | n | marker_miss_with_delivery |")
    lines.append("| --- | --- | --- | --- | --- |")
    for s in report.stats:
        count_display = (
            "n/a" if s.marker_miss_with_delivery is None else str(s.marker_miss_with_delivery)
        )
        lines.append(
            f"| {s.probe_class} | {s.corpus_scale} | {s.arm} | {s.n} | {count_display} |"
        )
    lines.append("")

    lines.append("## Follow-through hop delivered (issue athenaeum#1844, report_only)")
    lines.append("")
    lines.append(
        "`follow_hop_rate` is the share of `follow_through` cells in that group where every "
        "page named by `corpus.deep_hop_uids` -- the page an arm can only have landed on by "
        "FOLLOWING the body `[[wikilink]]`, never by a lexical match on the query -- was "
        "delivered to the arm, read from `_delivered_uids` (the grader's own dispatch). It "
        "separates the two causes a `False` `correctness_rate` conflates on this class: the "
        "arm never reached the second page (counted here as 0) versus it reached the page "
        "and the answer still did not carry the fact (counted here as 1, alongside a "
        "`marker_miss_with_delivery` of 1). The deep page is DERIVED from the link graph, "
        "never read off a fixed `expected_uids` position. `report_only`: this column feeds "
        "no §7 condition, no cutoff, no `compute_verdicts` input and no win/loss field, "
        "exactly as `marker_miss_with_delivery` above does not. `n/a` for every non-"
        "`follow_through` group -- no hop to have followed is a different fact from a rate "
        "of zero."
    )
    lines.append("")
    lines.append("| probe_class | corpus_scale | arm | n | follow_hop_rate |")
    lines.append("| --- | --- | --- | --- | --- |")
    for s in report.stats:
        lines.append(
            f"| {s.probe_class} | {s.corpus_scale} | {s.arm} | {s.n} | "
            f"{_fmt(s.follow_hop_rate)} |"
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

    lines.append("## Write path (Phase 2, athenaeum#1726) -- native memories filed by librarian")
    lines.append("")
    lines.append(
        "Issue athenaeum#1830 (operator ruling, Kromatic-Innovation/athenaeum#1791 comment "
        "5732689494): the librarian is not a fact writer and never acts as one in production "
        "-- it files what Claude has already written. The `athenaeum` row below is the "
        "NATIVE writer's own memory files, materialised as auto-memory intake and compiled "
        "by the librarian -- \"Claude's memories versus Claude's memories after filing,\" not "
        "raw observations compiled directly (`tests.evals.write_path.compile_observation_stream` "
        "still exists as a diagnostic; it feeds no row here). Scoped to observations carrying "
        "at least one planted token (`observations_measured` of `observations_total`); an "
        "observation with no token plants nothing this scanner can check. `n/a` means no "
        "token-bearing observation was present for that system/scale, never a fabricated 0."
    )
    lines.append("")
    if report.write_path_stats:
        # issue athenaeum#1785 (Quine review of PR#1813, should-fix 1): a
        # trailing "partial" column, not a silently-clean row, for any
        # (system, corpus_scale) pair whose numbers came from a deadline-
        # tripped (exit 75) athenaeum compile.
        if report.phase2_partial:
            lines.append(
                "_rows marked `partial` came from a deadline-tripped (exit 75) athenaeum "
                "compile -- real but incomplete progress, not a clean run._"
            )
            lines.append("")
        lines.append(
            "_`transient_retained` (issue athenaeum#1824/#1830) is the one column where LOWER "
            "is better: it counts planted tokens from `retain=False` observations -- a "
            "temporary outage, a point-in-time status, a task-scoped instruction -- that were "
            "filed DURABLY (neither a short decay bucket nor a near-term `valid_until`). A "
            "transient token that is absent, or filed with a reasonable decay, grades correct "
            "and is not counted here. It is scored against its own denominator "
            "(`transient_total`) and excluded from every retention column. `lost_token_ids` "
            "names the durable tokens missing from `answer_tokens_retained` (issue "
            "athenaeum#1830, \"name the lost facts\")._"
        )
        lines.append("")
        lines.append(
            "_`decay_correct` and `durable_overdecayed` (issue athenaeum#1841) grade decay "
            "CORRECTNESS, not decay presence. `decay_correct` counts the transient tokens "
            "filed under EXACTLY the bucket the observation expected (HIGHER is better, out "
            "of `transient_total`); a token filed `weekly` when `daily` was expected still "
            "grades correct under `transient_retained` -- a reasonable decay -- but is not "
            "counted here, so the two columns can legitimately disagree. `n/a` means the "
            "measurement was unavailable, never a fabricated 0: the store declares no decay "
            "bucket anywhere (the native arm has no frontmatter vocabulary) or no transient "
            "token carried an expectation. `durable_overdecayed` is the SECOND "
            "lower-is-better column on this table, alongside `transient_retained`: it counts "
            "DURABLE (`retain=True`) tokens filed on a page whose bucket expires -- a fact "
            "the store has scheduled to forget, the over-decay failure weighted equally with "
            "under-decay. 0 is the good result._"
        )
        lines.append("")
        lines.append(
            "| system | corpus_scale | pages_targeted | pages_written | answer_tokens_total | "
            "answer_tokens_retained | transient_total | transient_retained | decay_correct | "
            "durable_overdecayed | "
            "observations_total | observations_measured | "
            "observations_dropped | lost_token_ids | partial |"
        )
        lines.append(
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- "
            "| --- | --- | --- |"
        )
        for w in report.write_path_stats:
            partial = "yes" if (w.system, w.corpus_scale) in report.phase2_partial else "no"
            lost = ", ".join(w.lost_token_ids) if w.lost_token_ids else "none"
            lines.append(
                f"| {w.system} | {w.corpus_scale} | {w.pages_targeted} | "
                f"{w.pages_written if w.pages_written is not None else 'n/a'} | "
                f"{w.answer_tokens_total} | "
                f"{w.answer_tokens_retained if w.answer_tokens_retained is not None else 'n/a'} | "
                f"{w.transient_total} | "
                f"{w.transient_retained if w.transient_retained is not None else 'n/a'} | "
                f"{w.decay_correct if w.decay_correct is not None else 'n/a'} | "
                f"{w.durable_overdecayed} | "
                f"{w.observations_total} | {w.observations_measured} | "
                f"{w.observations_dropped if w.observations_dropped is not None else 'n/a'} | "
                f"{lost} | {partial} |"
            )
    else:
        lines.append("_no Phase 2 write-path data in this run_")
    lines.append("")

    lines.append("### Filing loss (issue athenaeum#1830)")
    lines.append("")
    lines.append(
        "A second, SEPARATE retention row: the compiled store measured against the tokens "
        "the NATIVE writer itself already retained, not against the full observation stream "
        "-- so filing loss is distinguishable from Claude's own write loss (see "
        "`compute_filing_loss_stats`). A token Claude never wrote down at all is excluded "
        "from `native_tokens_total` entirely; it is a native write loss, already visible on "
        "the ordinary table above, not a filing loss."
    )
    lines.append("")
    if report.filing_loss_stats:
        lines.append(
            "| system | corpus_scale | native_tokens_total | filed_tokens_retained | "
            "lost_token_ids |"
        )
        lines.append("| --- | --- | --- | --- | --- |")
        for f in report.filing_loss_stats:
            lost = ", ".join(f.lost_token_ids) if f.lost_token_ids else "none"
            lines.append(
                f"| {f.system} | {f.corpus_scale} | {f.native_tokens_total} | "
                f"{f.filed_tokens_retained if f.filed_tokens_retained is not None else 'n/a'} | "
                f"{lost} |"
            )
    else:
        lines.append("_no filing-loss data in this run_")
    lines.append("")

    lines.append("## Crossover scale (athenaeum#1725)")
    lines.append("")
    lines.append(
        "The smallest scale, per probe class, at which Athenaeum's correctness exceeds "
        "native's -- \"the number the decision turns on\" (design doc "
        "`docs/design/native-memory-baseline.md` §6/§7). Walks the SIZE axis only "
        f"({' < '.join(f'`{s}`' for s in SIZE_SCALE_ORDER)}); the "
        "`medium_dense`/`medium_verydense` "
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
