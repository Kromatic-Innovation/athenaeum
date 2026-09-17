# SPDX-License-Identifier: Apache-2.0
"""Pure set-arithmetic relevance metrics (issue athenaeum#1782).

No model calls, no I/O: every function here operates on uid sets already
computed by a retriever (grep baseline, a real ``recall_search`` call, or
the hook's breadcrumb output) -- see
``tests/evals/test_recall_covers_grep.py`` for the real callers and the
markdown tables built from these primitives.

Split out of ``test_recall_covers_grep.py`` (issue athenaeum#1782's own
scope note: "building on athenaeum#1770's three retrievers", same file per
eval-wave-2-spec.md section5.1 -- not a second test file) only because the
precision/contamination/cap-signal arithmetic plus its own unit tests made
that module too large to read as one piece. This module carries no test
collection of its own (not ``test_*.py``); its unit tests live in
``test_recall_covers_grep.py``.

**Contamination's denominator is a declared choice, not the issue's own AC
table wording (athenaeum#1782/athenaeum#1783 Quine review).** Issue
athenaeum#1782's acceptance-criteria table wrote
``contamination@R = |retrieved ∩ must_not_rank| / |retrieved|``. This
module instead computes ``|retrieved ∩ must_not_rank| / |must_not_rank|``
(:meth:`ProbeRelevance.contamination`) -- dividing by the SIZE OF THE
NEGATIVE SET, not by how much the retriever returned. The AC's own
formula, read literally, is undefined (division by zero) whenever a
retriever returns nothing, and -- worse -- goes vacuously to ``0.0`` for a
retriever that returns plenty but happens to avoid every ``must_not_rank``
uid, which reads as "no contamination measured" identically to "no
contamination present", collapsing two different findings into one number.
Dividing by ``|must_not_rank|`` instead answers "what fraction of the
known negative controls did this retriever surface", is well-defined for
any non-empty ``must_not_rank`` set regardless of how much was retrieved,
and only needs the SEPARATE, already-documented ``n/a`` guard for probes
with no authored ``must_not_rank`` set at all (issue athenaeum#1777's
finding) -- never a silent 1.0 off an EMPTY negative set, which is the one
vacuous case the AC text was actually trying to rule out. Named here the
same way :data:`CAP_SIGNAL_EPS` is named: this module's own choice,
declared so athenaeum#1783's ruling reads the real formula rather than the
AC table's.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Per-probe, per-retriever measurement
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProbeRelevance:
    """One probe's relevance measurement for ONE retriever.

    ``retrieved`` is whatever that retriever returned (ranked or not --
    duplicates and order are irrelevant to every metric here, which is why
    :attr:`retrieved_set` dedupes immediately). ``expected`` and
    ``must_not_rank`` are the probe's own ground truth
    (``Probe.expected_uids`` / ``Probe.must_not_rank``).
    """

    expected: frozenset[str]
    must_not_rank: frozenset[str]
    retrieved: tuple[str, ...]

    @property
    def retrieved_set(self) -> frozenset[str]:
        return frozenset(self.retrieved)

    @property
    def hits(self) -> frozenset[str]:
        return self.expected & self.retrieved_set

    @property
    def contaminants(self) -> frozenset[str]:
        return self.must_not_rank & self.retrieved_set

    def recall(self) -> float:
        """``|retrieved ∩ expected| / |expected|``. Raises on an abstention
        probe (``expected`` empty) -- callers filter those out upstream, the
        same non-abstention filter ``test_recall_covers_grep.py`` already
        applies via ``_non_abstention_probes``."""
        if not self.expected:
            raise ValueError("recall is undefined for a probe with no expected_uids")
        return len(self.hits) / len(self.expected)

    def precision(self) -> float | None:
        """``|retrieved ∩ expected| / |retrieved|``. ``None`` ("n/a") when
        the retriever returned nothing at all -- never a silent ``0.0``
        that would read as "retrieved everything wrong" when really nothing
        was retrieved."""
        if not self.retrieved_set:
            return None
        return len(self.hits) / len(self.retrieved_set)

    def contamination(self) -> float | None:
        """``|retrieved ∩ must_not_rank| / |must_not_rank|`` -- fraction of
        the KNOWN NEGATIVE CONTROLS this retriever surfaced. This is a
        deliberate departure from issue athenaeum#1782's own AC table,
        which wrote ``/ |retrieved|``; see the module docstring for why.
        ``None`` ("n/a") when the probe carries no authored
        ``must_not_rank`` set -- issue athenaeum#1782's own acceptance
        criterion: never a silent ``1.0`` (vacuously "0 surfaced of 0") off
        an empty negative set."""
        if not self.must_not_rank:
            return None
        return len(self.contaminants) / len(self.must_not_rank)


def grep_reachable_miss(grep: ProbeRelevance, other: ProbeRelevance) -> frozenset[str]:
    """Expected uids the grep baseline reaches that *other* (recall@5 or
    hook@3, measured against the SAME probe) does not."""
    return (grep.expected & grep.retrieved_set) - other.retrieved_set


# ---------------------------------------------------------------------------
# Pooling -- micro-averaged (sum hits / sum denominators, then divide once),
# matching test_print_per_probe_class_summary's existing convention of
# summing counts across probes in a group rather than averaging per-probe
# ratios (a probe with 1 expected page would otherwise weigh as much as one
# with 4).
# ---------------------------------------------------------------------------


@dataclass
class PooledRelevance:
    n_probes: int = 0
    expected_total: int = 0
    hit_total: int = 0
    retrieved_total: int = 0
    precision_na_count: int = 0
    must_not_rank_eligible: int = 0
    must_not_rank_total: int = 0
    contaminant_total: int = 0
    contamination_na_count: int = 0
    grep_reachable_miss_total: int = 0

    def add(self, pr: ProbeRelevance, *, miss: frozenset[str] = frozenset()) -> None:
        self.n_probes += 1
        self.expected_total += len(pr.expected)
        self.hit_total += len(pr.hits)
        self.retrieved_total += len(pr.retrieved_set)
        if not pr.retrieved_set:
            self.precision_na_count += 1
        if pr.must_not_rank:
            self.must_not_rank_eligible += 1
            self.must_not_rank_total += len(pr.must_not_rank)
            self.contaminant_total += len(pr.contaminants)
        else:
            self.contamination_na_count += 1
        self.grep_reachable_miss_total += len(miss)

    @property
    def recall(self) -> float | None:
        return self.hit_total / self.expected_total if self.expected_total else None

    @property
    def precision(self) -> float | None:
        return self.hit_total / self.retrieved_total if self.retrieved_total else None

    @property
    def contamination(self) -> float | None:
        return (
            self.contaminant_total / self.must_not_rank_total if self.must_not_rank_total else None
        )


def pool(
    measurements: Iterable[ProbeRelevance], misses: Sequence[frozenset[str]] | None = None
) -> PooledRelevance:
    agg = PooledRelevance()
    measurements = list(measurements)
    misses = list(misses) if misses is not None else [frozenset()] * len(measurements)
    for pr, miss in zip(measurements, misses, strict=True):
        agg.add(pr, miss=miss)
    return agg


def fmt(value: float | None) -> str:
    """Render a metric for a markdown cell: 4 significant figures, or the
    literal string ``n/a`` -- never a bare ``None`` or a silent ``0.0``."""
    return "n/a" if value is None else f"{value:.2f}"


def fmt_rpc(pooled: PooledRelevance) -> str:
    """``recall/precision/contamination`` for one markdown cell."""
    return f"{fmt(pooled.recall)}/{fmt(pooled.precision)}/{fmt(pooled.contamination)}"


# ---------------------------------------------------------------------------
# Cap signal (eval-wave-2-spec.md section5.3, read by athenaeum#1783's ruling)
# ---------------------------------------------------------------------------

#: "Material" gap threshold for the epic's cap-trigger condition. The epic
#: (eval-wave-2-spec.md section5.3) states the SHAPE of the trigger
#: ("materially below" / "falling sharply") but fixes no numeric value --
#: 0.05 (5 points of pooled recall/precision) is this issue's own choice,
#: named here so athenaeum#1783's ruling can adopt, tighten, or replace it
#: explicitly rather than inherit it silently.
CAP_SIGNAL_EPS = 0.05


def cap_verdict(
    *,
    recall_hook3: float | None,
    recall_recall5: float | None,
    precision_hook3: float | None,
    precision_recall5: float | None,
) -> str:
    """eval-wave-2-spec.md section5.3's trigger condition, evaluated literally
    off the pooled table -- not argued:

    * ``"fixed cap is cutting signal"`` -- recall@hook@3 materially below
      recall@recall-default WHILE precision@recall-default stays at or
      above precision@hook@3 (a literal ``>=``, no epsilon -- the epic's own
      wording is "stays at or above", not "stays materially at or above"):
      the cap discards relevant pages and buys nothing for it.
    * ``"mixed: cutting both"`` (issue athenaeum#1782/athenaeum#1783 Quine
      review) -- recall@hook@3 is materially below recall@recall-default
      (the SAME condition as "cutting signal" above) but precision@hook@3
      is HIGHER, not lower-or-equal: the cap is discarding relevant pages
      AND buying real precision at the same time, so calling that "cutting
      signal" alone -- as the first release of this function did -- reads
      the recall cost as free when it measurably was not. This is the
      common case on this issue's own fts5 rows: hook@3 costs recall
      relative to recall@5 while precision rises.
    * ``"fixed cap is cutting noise"`` -- recall HOLDS (no material drop)
      while precision@hook@3 is sharply above precision@recall-default:
      truncating to three buys precision at no material recall cost.
    * ``"inconclusive"`` otherwise -- recall holds and precision does not
      rise sharply either; nothing here supports a policy call.

    These four branches are mutually exclusive by construction (each
    checks ``recall`` first, then ``precision``, in a single if/elif
    chain), so -- unlike a first draft of this function, which flagged a
    same-turn "both conditions fire" case as a fifth, unreachable
    "inconclusive" path -- there is no dead branch to special-case here.

    Any ``None`` input (an empty pooled group -- no probes contributed a
    defined recall/precision for one of the two retrievers) is
    ``"inconclusive"`` by construction: there is nothing to trigger on.
    """
    if None in (recall_hook3, recall_recall5, precision_hook3, precision_recall5):
        return "inconclusive"
    assert recall_hook3 is not None
    assert recall_recall5 is not None
    assert precision_hook3 is not None
    assert precision_recall5 is not None
    material_recall_drop = recall_hook3 < recall_recall5 - CAP_SIGNAL_EPS
    material_precision_gain = precision_hook3 - precision_recall5 > CAP_SIGNAL_EPS
    if material_recall_drop and precision_recall5 >= precision_hook3:
        return "fixed cap is cutting signal"
    if material_recall_drop:
        return "mixed: cutting both"
    if material_precision_gain:
        return "fixed cap is cutting noise"
    return "inconclusive"


# ---------------------------------------------------------------------------
# name -> uid collision handling (issue athenaeum#1790)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NameToUidResult:
    mapping: dict[str, str]
    colliding_names: int
    colliding_pages: int


def build_name_to_uid(pages: Iterable[object]) -> NameToUidResult:
    """``name -> uid``, excluding any name shared by more than one page
    (issue athenaeum#1790: the medium-scale ballast/distractor tiers repeat
    templated names, and a plain ``{page.name: page.uid}`` dict
    comprehension silently resolves a collided breadcrumb name to whichever
    page iterated last).

    The audit's own suggested fallback -- resolve via description match --
    is unavailable here: ``tests.evals.corpus.Page`` has no ``description``
    field at all (never emitted by ``Page.to_markdown``), so every page in
    this corpus renders description-less regardless of scale. Exclusion is
    therefore the only sound resolution available to this fixture; a real
    wiki with authored descriptions could do better, which is why this is
    documented here rather than silently assumed.

    *pages* items need only a ``.name`` and ``.uid`` attribute (typed
    ``object`` to avoid importing ``tests.evals.corpus.Page`` and creating a
    cycle -- callers pass real ``Page`` instances).
    """
    by_name: dict[str, list[str]] = {}
    for page in pages:
        by_name.setdefault(page.name, []).append(page.uid)  # type: ignore[attr-defined]
    mapping: dict[str, str] = {}
    colliding_names = 0
    colliding_pages = 0
    for name, uids in by_name.items():
        if len(uids) == 1:
            mapping[name] = uids[0]
        else:
            colliding_names += 1
            colliding_pages += len(uids)
    return NameToUidResult(
        mapping=mapping, colliding_names=colliding_names, colliding_pages=colliding_pages
    )
