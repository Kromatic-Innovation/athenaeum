# SPDX-License-Identifier: Apache-2.0
"""Synthetic knowledge corpus: loader, materializer, and generator.

The eval corpus exists so retrieval and agent-level evals run against a
knowledge tree with KNOWN ground truth, at a controllable scale, without any
content from a live knowledge tree ever entering the repository.

Three tiers, and the distinction between them is the whole design:

``core``
    Hand-authored in ``data/corpus/core/*.yaml``. Carries every ground-truth
    assertion any eval makes. Committed, and auditable by reading those files
    top to bottom -- that is why the corpus is authored as a handful of world
    files rather than a hundred loose markdown pages.

``distractor``
    Generated. For each probe, pages that deliberately SHARE the probe's
    vocabulary without containing its answer. This tier creates retrieval
    pressure.

``ballast``
    Generated. Topic-diverse pages that make the index the right SIZE, so
    index-level effects (BM25 IDF shifts, vector neighbourhood crowding) are
    real rather than simulated.

Why ``distractor`` and ``ballast`` are separate tiers, separately controlled:
raw page count is probably NOT the variable that degrades recall. Ballast
alone does not compete for rank -- BM25 will not surface a page sharing no
terms with the query -- so a corpus scaled only with ballast can reach 10k
pages with recall@k untouched, and would license the wrong conclusion that
scale is harmless. Near-miss pages are what actually crowd out a correct
answer. Holding the axes apart is what lets a result distinguish "the corpus
is too big" from "the corpus is too confusable" -- different problems with
different fixes (a better index vs better disambiguation).

Relatedness and redundancy (issue athenaeum#1570) are layered onto ``core``
rather than made a fourth tier: they carry ground-truth assertions, which is
``core``'s definition, and they generate nothing, which is what the other two
tiers exist to control. See :class:`UnlinkedCluster` / :class:`RedundantCluster`
below and ``data/corpus/README.md``.

Generation is DETERMINISTIC and LLM-free: a seeded PRNG slot-fills committed
templates. The same ``(GENERATOR_VERSION, seed, scale)`` triple yields a
byte-identical tree, so large corpora are reproduced on demand and never
enter git. An LLM generator was rejected deliberately: it would cost per page
at 10k scale, make runs unreproducible, and -- the real reason -- an LLM
writing "realistic" pages is precisely the paraphrase-leakage path the
content policy forbids.

Content policy (see ``data/corpus/README.md``): every name here is invented.
Nothing is copied, paraphrased, or sampled from a live knowledge tree; only
DISTRIBUTION PARAMETERS are taken from one, via
``scripts/measure_corpus_shape.py``, whose output is gitignored.
``tests/test_eval_corpus_leakage.py`` enforces this on every PR.
"""

from __future__ import annotations

import hashlib
import inspect
import random
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import yaml

#: Matches a page body's plain ``Internal reference tag: <token>.`` line
#: (issue athenaeum#1759), used by :meth:`Page.to_markdown` to render it as
#: its own bold final line. Deliberately anchored on the literal prefix only
#: -- the same prefix ``validate_core`` and ``REFERENCE_TAG_INSTRUCTION``
#: both name -- so ``Page.body`` stays matchable in plain text while the
#: rendered markdown a model actually reads gets the unmistakable form.
_TAG_LINE_RE = re.compile(r"^Internal reference tag: (.+)$", re.MULTILINE)

#: A short, deliberately conservative stopword list -- excluded so a
#: lexical/n-gram overlap is not dominated by function words that would
#: overlap between almost any two English passages regardless of topic.
#: NOT a general-purpose NLP stopword list (no external dependency is
#: pulled in for this); just enough to keep the free metrics meaningful.
#:
#: Lives here, not in ``tests.evals.north_star_report`` (issue athenaeum#1737),
#: because ``validate_core``'s ``follow_through`` check needs the SAME content-
#: term definition ``north_star_report.lexical_overlap`` uses, and
#: ``north_star_report`` already imports from this module -- a definition
#: living there would make the reverse import circular. Only
#: :func:`_content_terms` (the public surface built on this list and
#: :data:`_WORD_RE` below) is re-imported into ``north_star_report`` under
#: its original name, so no external caller of THAT function needed to
#: change; ``_STOPWORDS``/``_WORD_RE`` themselves are not re-exported and
#: have exactly one definition, here.
_STOPWORDS = frozenset(
    {
        "the",
        "a",
        "an",
        "and",
        "or",
        "of",
        "to",
        "in",
        "on",
        "for",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "with",
        "as",
        "at",
        "by",
        "it",
        "this",
        "that",
        "these",
        "those",
        "from",
        "not",
        "no",
        "do",
        "does",
        "did",
        "what",
        "which",
        "who",
        "how",
        "when",
        "where",
        "why",
    }
)
_WORD_RE = re.compile(r"[a-z0-9]+")


def _content_terms(text: str) -> set[str]:
    words = _WORD_RE.findall(text.lower())
    return {w for w in words if len(w) >= 3 and w not in _STOPWORDS}


def _shares_stemmed_term(a: set[str], b: set[str]) -> bool:
    """True if some term in *a* and some term in *b* share a >=5-character
    prefix -- a cheap stand-in for real stemming, used by ``validate_core``'s
    ``follow_through`` check (issue athenaeum#1737) to catch a page whose
    ``uid``/``name``/``tags`` leak the query's vocabulary through a simple
    suffix variation an exact ``_content_terms`` intersection misses
    (``subsidiary`` vs ``subsidiaries``), without pulling in a real stemming
    dependency. Terms under 5 characters never match this way -- the exact
    intersection already covers short exact matches.
    """
    prefixes_a = {t[:5] for t in a if len(t) >= 5}
    return any(len(t) >= 5 and t[:5] in prefixes_a for t in b)


#: Same wikilink grammar as ``athenaeum.mcp_server._WIKILINK_RE`` /
#: ``athenaeum.inference_blocks._WIKILINK_RE`` / ``athenaeum.resolutions._WIKILINK_RE``
#: (Obsidian-style ``[[slug]]`` / ``[[slug|alias]]``) -- duplicated rather
#: than imported, matching this module's own precedent for this exact
#: regex (see ``mcp_server.py``'s comment above its copy): this module is
#: imported by nearly every eval test, and importing the full
#: MCP-SDK-dependent ``athenaeum.mcp_server`` module just for one regex would
#: drag that import graph into every one of them.
_WIKILINK_RE = re.compile(r"\[\[([^\[\]|\n]+?)(?:\|[^\[\]\n]*)?\]\]")


def _body_wikilink_targets(body: str) -> list[str]:
    """Wikilink targets in a page BODY, order-preserved, deduped -- the same
    shape ``athenaeum.mcp_server._extract_outbound_links`` reads to render a
    live recall hit's ``**Links:**`` line.

    ``validate_core``'s ``follow_through`` check (issue athenaeum#1737 Quine
    finding) parses THIS, not the frontmatter ``related``/``links`` edges a
    page also carries: the real recall snippet is rendered from the body
    only -- frontmatter ``related``/``links`` never reaches an agent through
    ``recall`` at all, only through a native arm's grep of the raw
    materialized file. A probe whose qualifying edge sat only in
    frontmatter would be passable by grep and structurally unpassable by
    Athenaeum -- exactly the asymmetry this probe class exists to catch, not
    exhibit. Frontmatter ``related``/``links`` stay on the page regardless
    (the relatedness/redundancy ground truth in this module still reads
    them), this check just does not accept them as the qualifying edge.
    """
    seen: set[str] = set()
    targets: list[str] = []
    for m in _WIKILINK_RE.finditer(body):
        raw = m.group(1).strip()
        if not raw or raw in seen:
            continue
        seen.add(raw)
        targets.append(raw)
    return targets


# Bump when generation logic changes in a way that alters emitted bytes for a
# fixed seed. Recorded alongside every result so a stored measurement names
# the corpus it was actually taken against.
# Bumped to 2 by issue athenaeum#1570: ``related:`` is now RENDERED into
# frontmatter, so every page carrying an authored edge emits different bytes
# than it did at version 1.
GENERATOR_VERSION = 2

CORPUS_ROOT = Path(__file__).parent / "data" / "corpus"
CORE_DIR = CORPUS_ROOT / "core"
TEMPLATE_DIR = CORPUS_ROOT / "templates"
PROBES_PATH = CORPUS_ROOT / "probes" / "probes.yaml"


@dataclass(frozen=True, order=True)
class RelatedEdge:
    """One outgoing ``related:`` edge, in the live ``{uid, role}`` shape.

    A frozen dataclass rather than a ``dict`` because :class:`Page` is frozen
    and hashable, and a tuple-of-dicts field would silently make it neither.
    :meth:`Page.to_markdown` renders these back out as mappings, which is the
    shape ``WikiEntity.related: list[dict[str, str]]``
    (``src/athenaeum/models.py:1587``) actually carries and the shape
    ``viewer_corpus._related_uids`` reads for breadcrumbs.
    """

    uid: str
    role: str = "related"


#: Role assigned to an edge authored through the ``links:`` shorthand.
#:
#: ``links:`` predates ``related:`` in this corpus and was, until athenaeum#1570,
#: a *separate* field that ``to_markdown`` never emitted -- authored ground
#: truth with no rendering. Rather than keep two edge concepts that can
#: diverge, ``links:`` is now pure authoring sugar: the loader folds it into
#: ``related`` under this role and :attr:`Page.links` is a read-only view back
#: over ``related``. Divergence is therefore impossible by construction rather
#: than merely tested for -- see ``test_eval_corpus_relatedness.py``.
LINK_ROLE = "mentions"


@dataclass(frozen=True)
class Page:
    """One wiki page, tier-tagged so evals can reason about provenance."""

    uid: str
    type: str
    name: str
    body: str
    tier: str
    aliases: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    access: str = "public"
    source_type: str = "user-stated"
    source_ref: str = "session-2026-01-01"
    created: str = "2026-01-01"
    updated: str = "2026-01-01"
    related: tuple[RelatedEdge, ...] = ()
    # Issue athenaeum#1493: the DECLARED supersession pointer (a page ``name:``
    # value, matching the real ``resolutions.py`` enactment convention — see
    # ``athenaeum.models.parse_superseded_by``'s docstring). "" (default)
    # emits no frontmatter key at all, so a page with no supersession story
    # renders byte-identical to before this field existed. This replaces the
    # corpus's prior body-text ``SUPERSEDED:`` convention (issue athenaeum#1493's
    # own finding: that convention was never a contract retrieval could
    # consult — see ``core/05-temporal.yaml``).
    superseded_by: str = ""

    def to_markdown(self) -> str:
        """Render frontmatter + body, matching the existing fixture shape.

        Scalar values are QUOTED. YAML 1.1 parses an unquoted ``9:00`` as the
        sexagesimal integer 540, so a page tagged with a time silently loads a
        non-string tag. That is not a hypothetical: it surfaced here as a hard
        ``TypeError`` inside ``recall_search`` on the FTS5 path, from a
        distractor carrying a ``9:00`` tag. Fixtures must emit unambiguous
        YAML so an eval measures retrieval rather than a parser accident.

        The ``Internal reference tag:`` line is rendered BOLD and alone on
        its own final line (issue athenaeum#1759) so it is the most
        identifier-shaped string on the rendered page -- the frontmatter
        ``uid:`` line was competing with it for that role, and 17 oracle
        cells in the first live grid cited the uid instead. ``self.body``
        itself is left untouched: ``validate_core`` and the corpus's own
        ``answer_tokens`` checks match the plain, unbolded
        ``Internal reference tag:`` prefix against ``Page.body``, not
        against this rendered markdown.
        """

        def q(value: str) -> str:
            return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'

        fm: list[str] = ["---", f"uid: {self.uid}", f"type: {self.type}"]
        fm.append(f"name: {q(self.name)}")
        if self.aliases:
            fm.append("aliases:")
            fm.extend(f"  - {q(a)}" for a in self.aliases)
        fm.append(f"access: {self.access}")
        if self.tags:
            fm.append("tags:")
            fm.extend(f"  - {q(t)}" for t in self.tags)
        # Emitted between ``tags`` and ``created``, matching where the live
        # ``WikiEntity.render()`` puts it (``models.py:1727``) -- the fixture
        # is only useful as a stand-in for a real wiki if it renders in the
        # real order. Block style, quoted scalars: same YAML 1.1 reasoning as
        # every other value here.
        if self.related:
            fm.append("related:")
            for edge in self.related:
                fm.append(f"  - uid: {q(edge.uid)}")
                fm.append(f"    role: {q(edge.role)}")
        fm.append(f"source_type: {self.source_type}")
        fm.append(f"source_ref: {q(self.source_ref)}")
        fm.append(f"created: {self.created}")
        fm.append(f"updated: {self.updated}")
        if self.superseded_by:
            fm.append(f"superseded_by: {q(self.superseded_by)}")
        fm.append("---")
        body_md = _TAG_LINE_RE.sub(r"**Internal reference tag:** \1", self.body.rstrip())
        return "\n".join(fm) + "\n\n" + body_md + "\n"

    @property
    def links(self) -> tuple[str, ...]:
        """Targets authored through the ``links:`` shorthand.

        A VIEW over :attr:`related`, never a second stored field. Issue
        athenaeum#1570 folded the two together precisely so a page cannot
        declare a body link the rendered ``related:`` block does not carry.
        """
        return tuple(edge.uid for edge in self.related if edge.role == LINK_ROLE)

    @property
    def filename(self) -> str:
        return f"{self.uid}.md"


#: Probe classes enrolled in design-doc §7 condition 2 today (issue
#: athenaeum#1776, athenaeum#1791 §2.1's ``report_only`` ruling). A probe
#: whose class is NOT in this set defaults ``report_only: True`` and stays
#: excluded from condition 2 (and condition 3) until an explicit operator
#: ruling, recorded on athenaeum#1736's thread, promotes it here -- by
#: editing THIS constant in a reviewable diff, never by flipping a
#: ``probes.yaml`` field alone. Fixed as of the wave-2 epic (athenaeum#1791);
#: adding a class here is what athenaeum#1776's whole guard exists to gate.
CONDITION_2_ENROLLED: frozenset[str] = frozenset(
    {
        "single_hop",
        "multi_hop",
        "disambiguation",
        "temporal",
        "abstention",
        "distractor_robustness",
        "redundancy",
        "follow_through",
    }
)

#: Probe classes introduced by eval wave 2 (issue athenaeum#1791) -- empty
#: until the sibling issues that add them (items D/E/F/G: athenaeum#1778
#: ``unprompted_push``, athenaeum#1780 ``aggregation``, athenaeum#1781
#: ``contradiction``/``negative_knowledge``) land and add their own class
#: name here. Which classes belong in this set is explicitly OUT OF SCOPE
#: for athenaeum#1776 (the issue that introduces the constant itself) --
#: decided by each class's own issue, not inferred from whatever is not
#: yet in :data:`CONDITION_2_ENROLLED`.
#:
#: Forward-declared, not yet read by :func:`validate_core`: the guard
#: implemented there is BLANKET (every probe's ``report_only`` must equal
#: ``probe_class not in CONDITION_2_ENROLLED``, not only probes whose class
#: is in this set) -- strictly stronger than scoping the check to this
#: constant, and correct today because it is empty. A sibling issue landing
#: a class here does not need to change the guard; it only needs to leave
#: that class's ``report_only`` unset (or ``True``) in ``probes.yaml``.
#: Grows to ``{"contradiction", "negative_knowledge"}`` with item G
#: (athenaeum#1781); sibling items D/F land their own class names here in
#: their own PRs, expect a rebase.
WAVE_2_PROBE_CLASSES: frozenset[str] = frozenset({"contradiction", "negative_knowledge"})


@dataclass(frozen=True)
class Probe:
    """A retrieval probe with its ground truth.

    ``probe_class`` follows the LongMemEval-style taxonomy: single_hop,
    multi_hop, temporal, disambiguation, abstention, distractor_robustness,
    follow_through.

    ``expected_uids`` is empty for abstention probes -- and that emptiness is
    the assertion, not a missing value. ``must_not_rank`` names pages that a
    correct system keeps OUT of the top-k, which is how a disambiguation
    probe states the failure it is guarding against.

    ``answer_tokens`` (issue athenaeum#1573) is the probe's ANSWER ground
    truth, distinct from ``expected_uids``'s RETRIEVAL ground truth: a
    unique, invented token planted in the body of one of the probe's own
    ``expected_uids`` pages, used for a normalized substring match against a
    rollout's final answer text (``tests.evals.north_star_report.grade_correctness``).
    Empty for abstention probes -- there, correctness is graded by a
    separate rule (no token to plant when nothing answers the probe).

    ``follow_through`` (issue athenaeum#1737) is the class a good grep cannot
    pass by accident: the query surfaces one page (a breadcrumb) whose
    complete answer requires following a wikilink to a SECOND page the
    query's own terms never reach. Distinguished from ``multi_hop`` (which
    is satisfied by two independently-retrievable pages) by every assertion
    below, all checked by :func:`validate_core`:

    * ``answer_tokens`` must be split across at least two ``expected_uids``
      pages -- a probe whose tokens all sit on one page has nothing to
      follow through TO.
    * the SOURCE page of the qualifying edge must itself share a content
      term with ``query`` -- otherwise nothing in ``expected_uids`` is
      lexically reachable from the query at all, and the probe cannot be a
      breadcrumb-then-follow shape by construction.
    * the qualifying edge must be a body ``[[wikilink]]``, not merely a
      frontmatter ``related``/``links`` entry: a live ``recall`` hit renders
      its ``**Links:**`` line from the body only
      (``athenaeum.mcp_server._extract_outbound_links``) -- frontmatter
      edges reach an agent only through a native arm's raw-file grep, so a
      frontmatter-only edge would make the class passable by grep and
      unpassable by Athenaeum by construction, the exact asymmetry it exists
      to catch.
    * the target page must carry a planted answer token, and its body,
      ``uid``, ``name``, and ``tags`` must share no content term (exact, or
      a >=5-character stemmed prefix -- see :func:`_shares_stemmed_term`)
      with ``query`` -- otherwise a plain BM25/lexical match, or a native
      arm's grep over its topic file's own name, would reach it directly
      without ever following the edge.

    ``multi_hop`` (issue athenaeum#1768) is looser than ``follow_through``:
    its two ``expected_uids`` pages are independently retrievable and no
    wikilink or breadcrumb-source overlap is required. But the page that
    actually carries the planted ``answer_tokens`` value must still not be
    reachable by the query's own vocabulary alone -- its body, ``uid``,
    ``name``, and ``tags`` must share no content term (exact, or the same
    ``_shares_stemmed_term`` stemmed prefix) with ``query``, checked by
    :func:`validate_core`. Without this, a query can be "complete" only with
    that page's fact and still land on it directly (by grep or by a plain
    lexical match), which defeats the two-hop shape the class exists to
    measure -- the answer no longer has to come FROM the second page, just
    happens to be gradable there.

    This check's scope is ``expected_uids`` only: it does not scan the rest
    of the core corpus for some OTHER page that also names the answer
    identity while sharing a query term (Quine review of the
    athenaeum#1768 PR found exactly this on `person-tomas-briell`, which
    named `ratecard_tooling_owner`'s answer person and shared "rate",
    "card", and "repository" with its query while sitting outside
    `expected_uids`, so the check above never looked at it). A probe author
    must keep the answer identity itself off every OTHER lexically
    reachable core page, not only off pages outside ``expected_uids`` that
    happen to share vocabulary -- CI cannot derive "the answer identity" as
    a general string to search for, so this is an authoring discipline the
    check does not enforce, the same shape as the ``follow_through``
    completeness judgment call documented in
    ``docs/design/native-memory-baseline.md`` §5.

    ``report_only`` (issue athenaeum#1776, athenaeum#1791 §2.1) excludes a
    probe from design-doc §7 condition 2 (and condition 3) -- reported
    alongside everything else, but never able to move the ratified kill
    criterion on its own. Parsed from ``probes.yaml``; when the yaml is
    silent, :func:`load_probes` defaults it to ``probe_class not in
    CONDITION_2_ENROLLED``, so a brand-new probe class is report-only
    unless the yaml says otherwise. :func:`validate_core` enforces the
    invariant in BOTH directions -- an enrolled class flagged
    ``report_only: True``, or a non-enrolled class left ``False`` -- so
    promotion into condition 2 is only ever a reviewable edit to the
    ``CONDITION_2_ENROLLED`` constant, never a ``probes.yaml`` field read
    by nobody.

    ``forbidden_tokens`` (issue athenaeum#1772, athenaeum#1791 §3.1) is the
    mirror image of ``answer_tokens``: a unique, invented token planted on a
    DECOY page, used by ``tests.evals.north_star_report.grade_harm`` to
    detect an answer that proposes the corpus's planted-wrong fact rather
    than merely missing the right one (the ``unprompted_push``/
    ``contradiction`` classes, items D/G -- this issue ships the mechanism
    only, no probe class uses the field yet). Empty for every probe today.
    :func:`validate_core` requires each value to be PLANTABLE (occur in the
    body of some corpus page), to collide with no probe's ``answer_tokens``
    value anywhere in the corpus (the same collision ``grade_correctness``'s
    abstention confabulation check already guards ``answer_tokens`` against
    -- a shared value would let a legitimate answer grade as harmful, or a
    harmful one grade as safe), and to be shared between no two pages.
    **Authoring rule (issue athenaeum#1811 Quine finding on athenaeum#1781):
    the forbidden token must BE the wrong answer's own name, stated in the
    decoy page's prose (a named person, endpoint, flag, vendor, or tool the
    page states as the fact) -- never a bolt-on marker line appended after
    the real stale content.** Nothing instructs a model to cite a decoy the
    way ``REFERENCE_TAG_INSTRUCTION`` instructs it to cite an answer page's
    tag; a model that lands on the stale page and applies its advice only
    ever reproduces words that are actually part of that advice, so a
    forbidden token sitting on a detached line is structurally unreachable
    by any answer a model would actually give, making ``grade_harm``
    unfireable regardless of what the model does.

    ``contradiction`` (issue athenaeum#1781, athenaeum#1791 §3.2) reuses
    ``must_not_rank`` to name a single STALE page (same ``superseded_by:``/
    body-``[[wikilink]]`` convention as ``05-temporal.yaml``) that a keyword
    search on the query plausibly lands on and that carries one of
    ``forbidden_tokens``; the page ``superseded_by`` resolves to must be in
    ``expected_uids`` and carry its own ``answer_tokens`` value. Two shapes
    share this mechanism: (a) supersession by an unfound page, where the
    superseding page is authored to share no content term with the query,
    so only the link reaches it; (b) the operator's deprecated-knowledge
    scenario, a TASK-phrased query that reaches a stale workaround page
    whose retraction links back to it. Correct means ``grade_correctness``
    AND ``grade_harm`` both pass -- the answer token cited and the
    forbidden token absent.

    ``negative_knowledge`` (issue athenaeum#1781, athenaeum#1791 §3.3, use
    case 2.4 "what went wrong last time?") is checked the INVERSE of the
    ``follow_through`` lexical-unreachability rule: at least one
    ``expected_uids`` page must share a content term (exact, or a stemmed
    prefix via ``uid``/``name``/``tags``) with the query -- deliberately
    grep-reachable, so a ``negative_knowledge`` probe whose retro page is
    lexically unreachable is a ``follow_through`` probe filed under the
    wrong class. ``forbidden_tokens`` sits on a separate "naive plan" decoy
    page that repeats the mistake the retro warns against.
    """

    id: str
    probe_class: str
    query: str
    expected_uids: tuple[str, ...] = ()
    must_not_rank: tuple[str, ...] = ()
    distractor_terms: tuple[str, ...] = ()
    answer_tokens: tuple[str, ...] = ()
    forbidden_tokens: tuple[str, ...] = ()
    note: str = ""
    report_only: bool = False
    #: Filtering metadata only (issue athenaeum#1779) -- parsed from
    #: ``probes.yaml``, defaults to ``"core"``, and read by nobody in the
    #: grid dispatch or ``compute_verdicts``. Lets an operator isolate the
    #: long-page slice (``tier: long``) without touching ``probe_class``,
    #: the same reasoning as ``Page.tier`` but kept independent of it --
    #: a probe's tier is about which probes to filter, a page's tier is
    #: about corpus provenance, and the two do not have to agree (a
    #: ``core``-tier probe's ``expected_uids`` could in principle span
    #: pages of different tiers).
    tier: str = "core"


@dataclass
class Corpus:
    """A materialized corpus: pages plus the probes they answer."""

    pages: list[Page] = field(default_factory=list)
    probes: list[Probe] = field(default_factory=list)
    seed: int = 0
    scale: str = "core"

    def tier_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for page in self.pages:
            counts[page.tier] = counts.get(page.tier, 0) + 1
        return counts

    def fingerprint(self) -> str:
        """Stable digest of the emitted tree.

        Recorded with every measurement so a result names the exact corpus it
        was taken against -- a seed alone is not enough once the generator or
        the hand-authored core changes.
        """
        digest = hashlib.sha256()
        digest.update(f"v{GENERATOR_VERSION}".encode())
        for page in sorted(self.pages, key=lambda p: p.uid):
            digest.update(page.uid.encode())
            digest.update(page.to_markdown().encode())
        return digest.hexdigest()[:16]

    def materialize(self, root: Path) -> Path:
        """Write the corpus to ``root`` as a wiki tree of markdown pages."""
        wiki = root / "wiki"
        wiki.mkdir(parents=True, exist_ok=True)
        for page in self.pages:
            (wiki / page.filename).write_text(page.to_markdown(), encoding="utf-8")
        return wiki


# --------------------------------------------------------------------------
# Loading the hand-authored core
# --------------------------------------------------------------------------


def _load_yaml(path: Path) -> Any:
    if not path.exists():
        raise FileNotFoundError(f"corpus file missing: {path}")
    return yaml.safe_load(path.read_text(encoding="utf-8")) or []


def _parse_related(raw: dict, path: Path) -> tuple[RelatedEdge, ...]:
    """Build one page's edge tuple from ``related:`` and/or ``links:``.

    Both spellings are accepted at authoring time and both land in the SAME
    field, so there is no second edge concept to keep in step:

    * ``related: [{uid: ..., role: ...}]`` -- the live shape, used where the
      role carries meaning (``models``, ``embodies``, ``implements``).
    * ``links: [uid, ...]`` -- the older shorthand, folded in under
      :data:`LINK_ROLE`.

    Order is preserved and duplicates by ``uid`` collapse to the first
    spelling seen, so authoring a uid in both blocks cannot emit it twice.
    """
    edges: list[RelatedEdge] = []
    seen: set[str] = set()

    for entry in raw.get("related", ()) or ():
        if not isinstance(entry, dict) or "uid" not in entry:
            raise ValueError(
                f"page {raw.get('uid')!r} in {path.name}: every `related:` entry "
                f"must be a mapping with a `uid:` key; got {entry!r}"
            )
        uid = str(entry["uid"])
        if uid in seen:
            continue
        seen.add(uid)
        edges.append(RelatedEdge(uid=uid, role=str(entry.get("role", "related"))))

    for target in raw.get("links", ()) or ():
        uid = str(target)
        if uid in seen:
            continue
        seen.add(uid)
        edges.append(RelatedEdge(uid=uid, role=LINK_ROLE))

    return tuple(edges)


def load_core_pages() -> list[Page]:
    """Load every hand-authored page from ``data/corpus/core/*.yaml``.

    Files are read in sorted order so the corpus is stable regardless of
    filesystem enumeration order -- a generated tree that varies by platform
    would break the fingerprint contract.
    """
    pages: list[Page] = []
    seen: dict[str, Path] = {}
    for path in sorted(CORE_DIR.glob("*.yaml")):
        for raw in _load_yaml(path):
            uid = raw["uid"]
            if uid in seen:
                raise ValueError(
                    f"duplicate uid {uid!r} in {path.name}; already defined in {seen[uid].name}"
                )
            seen[uid] = path
            pages.append(
                Page(
                    uid=uid,
                    type=raw["type"],
                    name=raw["name"],
                    body=raw["body"],
                    # Issue athenaeum#1779: every core page defaulted to
                    # tier "core" until the long-page tier needed a real
                    # per-page value -- `raw.get("tier", "core")` keeps that
                    # default for every existing core/*.yaml file (none of
                    # them carry a `tier:` key) while letting
                    # 12-long-pages.yaml declare `tier: long`.
                    tier=raw.get("tier", "core"),
                    aliases=tuple(raw.get("aliases", ())),
                    tags=tuple(raw.get("tags", ())),
                    access=raw.get("access", "public"),
                    source_type=raw.get("source_type", "user-stated"),
                    source_ref=raw.get("source_ref", "session-2026-01-01"),
                    created=raw.get("created", "2026-01-01"),
                    updated=raw.get("updated", raw.get("created", "2026-01-01")),
                    related=_parse_related(raw, path),
                    superseded_by=raw.get("superseded_by", ""),
                )
            )
    return pages


def load_probes() -> list[Probe]:
    """Load the probe set with its ground truth."""
    return [
        Probe(
            id=raw["id"],
            probe_class=raw["probe_class"],
            query=raw["query"],
            expected_uids=tuple(raw.get("expected_uids", ())),
            must_not_rank=tuple(raw.get("must_not_rank", ())),
            distractor_terms=tuple(raw.get("distractor_terms", ())),
            answer_tokens=tuple(raw.get("answer_tokens", ())),
            forbidden_tokens=tuple(raw.get("forbidden_tokens", ())),
            note=raw.get("note", ""),
            report_only=bool(
                raw.get("report_only", raw["probe_class"] not in CONDITION_2_ENROLLED)
            ),
            tier=raw.get("tier", "core"),
        )
        for raw in _load_yaml(PROBES_PATH)
    ]


def validate_core(pages: list[Page], probes: list[Probe]) -> list[str]:
    """Return human-readable problems with the hand-authored corpus.

    Checked here rather than at use time because a probe whose ``expected_uids``
    names a page that does not exist does not fail loudly -- it silently scores
    as a retrieval MISS, which reads as a model regression. A dangling
    ground-truth reference must be a corpus error, never an eval result.

    ``answer_tokens`` is checked the same way (issue athenaeum#1573): a
    non-abstention probe with no planted token, or a token that does not
    actually occur in any of its own ``expected_uids`` pages' bodies, would
    silently score as an un-gradable "n/a" correctness cell -- indistinguishable
    from a real floor/ceiling of zero -- rather than a corpus authoring error.

    ``follow_through`` probes (issue athenaeum#1737) get ADDITIONAL checks
    beyond the generic ones above (see the ``Probe`` docstring for the full
    list), because the class exists to rule
    out a shape a plain retrieval probe cannot detect: an answer that LOOKS
    multi-page but is actually reachable from the query directly, with no
    edge ever followed. Without the source-page half, a probe could be
    authored where NOTHING in ``expected_uids`` is lexically reachable from
    the query at all -- passing the no-overlap check on a technicality
    rather than because a real breadcrumb was followed.

    ``multi_hop`` probes (issue athenaeum#1768) get the lexical-unreachability
    half of that same check applied to just the page carrying the planted
    token (see the ``Probe`` docstring): unlike ``follow_through`` no
    breadcrumb/wikilink structure is required, since ``multi_hop``'s two
    pages are independently retrievable by design.

    ``report_only`` (issue athenaeum#1776) is checked in BOTH directions
    against :data:`CONDITION_2_ENROLLED`: an enrolled class flagged
    ``report_only: True``, or a non-enrolled class left ``False``, is a
    corpus error either way -- otherwise a class could be promoted into
    (or quietly dropped from) design-doc §7 condition 2 by editing a
    ``probes.yaml`` field nobody reads twice, instead of the reviewable
    ``CONDITION_2_ENROLLED`` constant edit that ruling requires.
    """
    uids = {p.uid for p in pages}
    pages_by_uid = {p.uid: p for p in pages}
    problems: list[str] = []
    for probe in probes:
        for uid in probe.expected_uids:
            if uid not in uids:
                problems.append(f"probe {probe.id!r}: expected_uids names unknown page {uid!r}")
        for uid in probe.must_not_rank:
            if uid not in uids:
                problems.append(f"probe {probe.id!r}: must_not_rank names unknown page {uid!r}")
        # Issue athenaeum#1777: `must_not_rank` names the failure a correct
        # system guards against -- a page that must NOT rank alongside the
        # correct answer. A uid appearing in BOTH `must_not_rank` and
        # `expected_uids` would grade retrieving it as simultaneously
        # correct and a contamination hit, which is not a precision
        # assertion at all, just a contradictory one.
        overlap = set(probe.must_not_rank) & set(probe.expected_uids)
        if overlap:
            problems.append(
                f"probe {probe.id!r}: must_not_rank and expected_uids both name "
                f"{sorted(overlap)}"
            )
        if probe.probe_class == "abstention" and probe.expected_uids:
            problems.append(f"probe {probe.id!r}: abstention probes must have no expected_uids")
        if probe.probe_class != "abstention" and not probe.expected_uids:
            problems.append(f"probe {probe.id!r}: no expected_uids and not abstention")
        if probe.probe_class in CONDITION_2_ENROLLED and probe.report_only:
            problems.append(
                f"probe {probe.id!r}: probe_class {probe.probe_class!r} is in "
                "CONDITION_2_ENROLLED but report_only is True -- demoting an enrolled class "
                "out of condition 2 requires editing CONDITION_2_ENROLLED, not a probes.yaml "
                "field alone"
            )
        if probe.probe_class not in CONDITION_2_ENROLLED and not probe.report_only:
            problems.append(
                f"probe {probe.id!r}: probe_class {probe.probe_class!r} is not in "
                "CONDITION_2_ENROLLED but report_only is False -- promotion into condition 2 "
                "is an explicit operator ruling (athenaeum#1736) recorded by adding the class "
                "to CONDITION_2_ENROLLED, never a probes.yaml field alone"
            )
        if probe.probe_class == "abstention" and probe.answer_tokens:
            problems.append(f"probe {probe.id!r}: abstention probes must have no answer_tokens")
        if probe.probe_class != "abstention" and not probe.answer_tokens:
            problems.append(f"probe {probe.id!r}: no answer_tokens and not abstention")
        elif probe.probe_class != "abstention":
            answer_text = "\n".join(
                pages_by_uid[uid].body for uid in probe.expected_uids if uid in pages_by_uid
            )
            if not any(token in answer_text for token in probe.answer_tokens):
                problems.append(
                    f"probe {probe.id!r}: no answer_tokens value found in its answer page body"
                )
            # Issue athenaeum#1759: a token can occur in a page body without
            # ever being ON the `Internal reference tag:` line the grading
            # contract and REFERENCE_TAG_INSTRUCTION both name -- that is
            # exactly how the follow_through fixture drifted to `Internal
            # reference code:` and passed the check above while being
            # unsatisfiable by a correct answer. Require each token to sit on
            # a line beginning with the literal prefix in at least one
            # expected_uids page.
            tag_lines = [
                line
                for uid in probe.expected_uids
                if uid in pages_by_uid
                for line in pages_by_uid[uid].body.splitlines()
                if line.strip().startswith("Internal reference tag:")
            ]
            for token in probe.answer_tokens:
                if not any(token in line for line in tag_lines):
                    problems.append(
                        f"probe {probe.id!r}: answer_tokens value {token!r} does not occur on "
                        "an 'Internal reference tag:' line of any expected_uids page"
                    )
        if probe.probe_class == "follow_through":
            expected_pages = [
                pages_by_uid[uid] for uid in probe.expected_uids if uid in pages_by_uid
            ]
            token_pages = {
                page.uid
                for page in expected_pages
                if any(token in page.body for token in probe.answer_tokens)
            }
            if len(token_pages) < 2:
                problems.append(
                    f"probe {probe.id!r}: follow_through answer_tokens must be split across "
                    "at least two expected_uids pages, not concentrated on one"
                )
            expected_uid_set = set(probe.expected_uids)
            query_terms = _content_terms(probe.query)
            has_qualifying_hop = False
            for page in expected_pages:
                if not (_content_terms(page.body) & query_terms):
                    # This page shares no vocabulary with the query either --
                    # it cannot be the breadcrumb a lexical match surfaces, so
                    # an edge leaving it would not demonstrate a real hop.
                    continue
                for target_uid in _body_wikilink_targets(page.body):
                    if target_uid == page.uid or target_uid not in expected_uid_set:
                        continue
                    target = pages_by_uid.get(target_uid)
                    if target is None:
                        continue
                    if _content_terms(target.body) & query_terms:
                        continue
                    target_meta_terms = _content_terms(
                        f"{target.uid.replace('-', ' ')} {target.name} {' '.join(target.tags)}"
                    )
                    if _shares_stemmed_term(target_meta_terms, query_terms):
                        # The page itself is grep-reachable from the query via
                        # its uid/name/tags (a native arm's topic file is named
                        # `<uid>.md`) even though its body is clean -- the
                        # no-overlap assertion must hold for the whole page a
                        # native arm would land on, not only its body text.
                        continue
                    if not any(token in target.body for token in probe.answer_tokens):
                        # The hop must land somewhere that actually carries a
                        # planted token -- an edge to a clean-but-token-free
                        # page would satisfy the shape without ever reaching
                        # the answer.
                        continue
                    has_qualifying_hop = True
            if not has_qualifying_hop:
                problems.append(
                    f"probe {probe.id!r}: follow_through probes need a body [[wikilink]] "
                    "(not just a frontmatter related/links edge) from an expected_uids page "
                    "that itself shares a content term with the query, to another "
                    "expected_uids page that carries a planted answer token and whose body, "
                    "uid, name, and tags share no content term (including a stemmed prefix) "
                    "with the query"
                )
        if probe.probe_class == "multi_hop":
            # Issue athenaeum#1768: the multi_hop counterpart of the
            # follow_through lexical-unreachability check above, scoped to
            # the page that actually carries the planted token (not every
            # expected_uids page -- multi_hop's other page is allowed, even
            # expected, to share the query's vocabulary; that is how a plain
            # lexical match surfaces it as the breadcrumb in the first
            # place). Without this, a query can be "complete" only with the
            # token page's fact yet still share enough vocabulary with that
            # page that a native arm's grep -- or a model that never visits
            # the token page at all -- lands on it directly, which is
            # exactly the `spend_approver_named` / `portal_design_reviewer`
            # defect the issue reports: the token page's own body restated
            # the query's terms, so nothing forced a real second hop.
            expected_pages = [
                pages_by_uid[uid] for uid in probe.expected_uids if uid in pages_by_uid
            ]
            query_terms = _content_terms(probe.query)
            token_pages = [
                page
                for page in expected_pages
                if any(token in page.body for token in probe.answer_tokens)
            ]
            for page in token_pages:
                if _content_terms(page.body) & query_terms:
                    problems.append(
                        f"probe {probe.id!r}: multi_hop token page {page.uid!r} shares a "
                        "content term with the query, so it is reachable by the query's own "
                        "vocabulary without following the hop"
                    )
                    continue
                page_meta_terms = _content_terms(
                    f"{page.uid.replace('-', ' ')} {page.name} {' '.join(page.tags)}"
                )
                if _shares_stemmed_term(page_meta_terms, query_terms):
                    problems.append(
                        f"probe {probe.id!r}: multi_hop token page {page.uid!r}'s uid, name, "
                        "or tags share a stemmed term with the query, so a native arm's grep "
                        "over its topic file's own name would reach it without the hop"
                    )
        if probe.probe_class == "aggregation":
            # Issue athenaeum#1780: the many-correct-answer class, graded by
            # `grade_coverage`'s matched FRACTION of `answer_tokens` rather
            # than `grade_correctness`'s all-or-nothing rule. Two failure
            # modes the generic checks above do not catch:
            #
            # 1. A probe with no negative control scores perfectly by
            #    naming everything a keyword search surfaces -- the same
            #    failure `RedundantCluster.negative_control` guards against
            #    (this module, `RedundantCluster` docstring). `must_not_rank`
            #    must be non-empty for every aggregation probe.
            # 2. `answer_tokens` concentrated on one `expected_uids` page (or
            #    missing from another) would make "coverage" collapse back
            #    into ordinary single-hop correctness -- the SAME
            #    concentration failure `follow_through`'s check above rejects
            #    for its own class, inverted here: aggregation wants the
            #    tokens SPREAD one-per-page, not proving a hop was followed.
            if not probe.must_not_rank:
                problems.append(
                    f"probe {probe.id!r}: aggregation probes must set must_not_rank naming "
                    "decoy pages sharing the query's vocabulary -- a many-answer probe with "
                    "no negative control scores perfectly by naming everything"
                )
            expected_pages = [
                pages_by_uid[uid] for uid in probe.expected_uids if uid in pages_by_uid
            ]
            token_owners: dict[str, set[str]] = {}
            for page in expected_pages:
                owning_tokens = [tok for tok in probe.answer_tokens if tok in page.body]
                if not owning_tokens:
                    problems.append(
                        f"probe {probe.id!r}: aggregation expected_uids page {page.uid!r} "
                        "carries none of the probe's answer_tokens -- grade_coverage cannot "
                        "credit a page with no planted token"
                    )
                for tok in owning_tokens:
                    token_owners.setdefault(tok, set()).add(page.uid)
            for tok, owners in token_owners.items():
                if len(owners) > 1:
                    problems.append(
                        f"probe {probe.id!r}: aggregation answer_tokens value {tok!r} occurs "
                        f"on multiple expected_uids pages {sorted(owners)} -- each token must "
                        "sit on a distinct page so coverage counts pages, not repeats"
                    )
        if probe.probe_class == "contradiction":
            # Issue athenaeum#1781, athenaeum#1791 §3.2. Both shapes -- (a)
            # supersession by an unfound page, and (b) the operator's
            # deprecated-knowledge scenario -- reuse `must_not_rank` to name
            # the single STALE/workaround page a keyword search plausibly
            # lands on, and the existing `superseded_by`/body-wikilink
            # convention (`05-temporal.yaml`) to name the correct page it
            # points at. The check is deliberately shape-agnostic: shape (a)
            # additionally authors its superseding page to share no content
            # term with the query (so the link is the ONLY way to reach it)
            # and shape (b) additionally has the retraction page link back
            # to the stale page -- neither is enforced generically here,
            # the same "authoring discipline the check does not enforce"
            # shape as the `multi_hop` answer-identity-uniqueness caveat
            # above (see the `Probe` docstring).
            if len(probe.must_not_rank) != 1:
                problems.append(
                    f"probe {probe.id!r}: contradiction probes must name exactly one "
                    "must_not_rank page (the stale/superseded page a keyword search "
                    "plausibly lands on)"
                )
            elif not probe.forbidden_tokens:
                problems.append(
                    f"probe {probe.id!r}: contradiction probes must carry forbidden_tokens "
                    "(planted on the stale must_not_rank page)"
                )
            else:
                stale_uid = probe.must_not_rank[0]
                stale_page = pages_by_uid.get(stale_uid)
                if stale_page is not None:
                    query_terms = _content_terms(probe.query)
                    stale_meta_terms = _content_terms(
                        f"{stale_page.uid.replace('-', ' ')} {stale_page.name} "
                        f"{' '.join(stale_page.tags)}"
                    )
                    if not (
                        (_content_terms(stale_page.body) & query_terms)
                        or _shares_stemmed_term(stale_meta_terms, query_terms)
                    ):
                        problems.append(
                            f"probe {probe.id!r}: contradiction's must_not_rank (stale) page "
                            f"{stale_uid!r} must be lexically reachable from the query -- a "
                            "keyword search that cannot even find the stale page proves "
                            "nothing about ranking it below the correct answer"
                        )
                    # Issue athenaeum#1811 Quine finding: the forbidden token must
                    # be the wrong answer's own name, stated as part of the stale
                    # prose (outside any tag line) -- a model that applies the stale
                    # fact then naturally echoes it, the same way the reference-tag
                    # instruction drives citation of a real answer token. A token
                    # sitting only on a bolt-on "Internal reference tag:"-shaped
                    # line gives a model applying the stale advice no reason to ever
                    # repeat it, making grade_harm structurally unfireable.
                    stale_non_tag_lines = "\n".join(
                        line
                        for line in stale_page.body.splitlines()
                        if not line.strip().startswith("Internal reference tag:")
                    )
                    if not any(token in stale_non_tag_lines for token in probe.forbidden_tokens):
                        problems.append(
                            f"probe {probe.id!r}: contradiction's must_not_rank page "
                            f"{stale_uid!r} does not carry any of the probe's forbidden_tokens "
                            "in its prose (outside any 'Internal reference tag:' line) -- the "
                            "forbidden token must be the wrong answer's own name, not a "
                            "bolt-on marker a model applying the stale fact has no reason to "
                            "repeat"
                        )
                    if not stale_page.superseded_by:
                        problems.append(
                            f"probe {probe.id!r}: contradiction's stale page {stale_uid!r} "
                            "must declare superseded_by"
                        )
                    else:
                        target_page = next(
                            (p for p in pages if p.name == stale_page.superseded_by), None
                        )
                        if target_page is None:
                            problems.append(
                                f"probe {probe.id!r}: contradiction's stale page {stale_uid!r} "
                                f"superseded_by {stale_page.superseded_by!r} does not resolve "
                                "to any page's name"
                            )
                        else:
                            if target_page.uid not in probe.expected_uids:
                                problems.append(
                                    f"probe {probe.id!r}: contradiction's stale page "
                                    f"{stale_uid!r} superseded_by resolves to "
                                    f"{target_page.uid!r}, which is not in expected_uids"
                                )
                            if not _TAG_LINE_RE.search(target_page.body):
                                problems.append(
                                    f"probe {probe.id!r}: contradiction's superseding page "
                                    f"{target_page.uid!r} carries no 'Internal reference tag:' "
                                    "line of its own"
                                )
                            if target_page.uid not in _body_wikilink_targets(stale_page.body):
                                problems.append(
                                    f"probe {probe.id!r}: contradiction's stale page "
                                    f"{stale_uid!r} must link to the superseding page "
                                    f"{target_page.uid!r} via a body [[wikilink]], not only "
                                    "superseded_by"
                                )
        if probe.probe_class == "negative_knowledge":
            # Issue athenaeum#1781, athenaeum#1791 §3.3 (use case 2.4). The
            # INVERSE of the `follow_through` lexical-unreachability check:
            # a `negative_knowledge` probe's retro/lesson page must share a
            # content term with the query (grep can find it) -- a probe
            # whose target is lexically unreachable is a `follow_through`
            # probe filed under the wrong class.
            if not probe.forbidden_tokens:
                problems.append(
                    f"probe {probe.id!r}: negative_knowledge probes must carry "
                    "forbidden_tokens (planted on a naive-plan decoy page)"
                )
            elif probe.must_not_rank:
                # Issue athenaeum#1811 Quine finding, same rule as contradiction
                # above: the forbidden token must be the naive plan's own wrong
                # answer, stated in prose (outside any tag line), not a bolt-on
                # marker nothing drives a model to repeat.
                naive_uid = probe.must_not_rank[0]
                naive_page = pages_by_uid.get(naive_uid)
                if naive_page is not None:
                    naive_non_tag_lines = "\n".join(
                        line
                        for line in naive_page.body.splitlines()
                        if not line.strip().startswith("Internal reference tag:")
                    )
                    if not any(
                        token in naive_non_tag_lines for token in probe.forbidden_tokens
                    ):
                        problems.append(
                            f"probe {probe.id!r}: negative_knowledge's must_not_rank page "
                            f"{naive_uid!r} does not carry any of the probe's "
                            "forbidden_tokens in its prose (outside any 'Internal reference "
                            "tag:' line) -- the forbidden token must be the naive plan's own "
                            "wrong answer, not a bolt-on marker"
                        )
            query_terms = _content_terms(probe.query)
            reachable = False
            for uid in probe.expected_uids:
                page = pages_by_uid.get(uid)
                if page is None:
                    continue
                page_meta_terms = _content_terms(
                    f"{page.uid.replace('-', ' ')} {page.name} {' '.join(page.tags)}"
                )
                if (_content_terms(page.body) & query_terms) or _shares_stemmed_term(
                    page_meta_terms, query_terms
                ):
                    reachable = True
                    break
            if not reachable:
                problems.append(
                    f"probe {probe.id!r}: negative_knowledge probes must have at least one "
                    "expected_uids page lexically reachable from the query (the inverse of "
                    "the follow_through check) -- otherwise this is a follow_through probe "
                    "filed under the wrong class"
                )
    for page in pages:
        for edge in page.related:
            if edge.uid not in uids:
                problems.append(f"page {page.uid!r}: related edge names unknown page {edge.uid!r}")

    # Issue athenaeum#1766 defect 2: a page named in some probe's
    # `expected_uids` but carrying no `Internal reference tag:` line has
    # nothing a model can cite for it, so a correct answer that draws on the
    # page falls back to citing its `uid` -- exactly the failure
    # `REFERENCE_TAG_INSTRUCTION` forbids and the grader cannot credit. The
    # athenaeum#1759 check above only requires a probe's OWN `answer_tokens`
    # to sit on a tag line; it says nothing about the other `expected_uids`
    # pages a multi_hop/follow_through answer also cites.
    expected_page_uids = {uid for probe in probes for uid in probe.expected_uids if uid in uids}
    for page_uid in sorted(expected_page_uids):
        tag_lines = _TAG_LINE_RE.findall(pages_by_uid[page_uid].body)
        if not tag_lines:
            problems.append(
                f"page {page_uid!r}: named in a probe's expected_uids but carries no "
                "'Internal reference tag:' line"
            )
        elif len(tag_lines) > 1:
            problems.append(
                f"page {page_uid!r}: carries {len(tag_lines)} 'Internal reference tag:' "
                "lines, expected exactly one"
            )

    # No two pages may share a token: `grade_correctness`'s abstention
    # confabulation check reads every planted token across the WHOLE corpus
    # (issue athenaeum#1759's docstring), so a collision would let an answer
    # that legitimately drew on one page's fact grade as confabulation
    # against an unrelated page's abstention probe, or let a correct answer
    # to one probe accidentally satisfy a different probe's token check.
    tag_owners: dict[str, list[str]] = {}
    for page in pages:
        for value in _TAG_LINE_RE.findall(page.body):
            tag_owners.setdefault(value.strip().rstrip("."), []).append(page.uid)
    for value, owners in sorted(tag_owners.items()):
        if len(owners) > 1:
            problems.append(f"tag {value!r} is used on multiple pages: {sorted(owners)}")

    # forbidden_tokens (issue athenaeum#1772): a forbidden token must be
    # PLANTABLE (occur in the body of some corpus page -- otherwise
    # `grade_harm` could never see it planted), must NOT occur on any of
    # its own probe's `expected_uids` pages (it belongs on a DECOY page,
    # never the correct-answer page -- an answer that legitimately cites
    # the right page would otherwise grade as harmful), must collide with
    # no probe's `answer_tokens` value anywhere in the corpus (the same
    # collision the tag-collision loop above guards `answer_tokens`
    # against -- a shared value would let a legitimate answer grade as
    # harmful, or a harmful one grade as safe), and must be shared between
    # no two pages.
    #
    # The `answer_tokens` collision check is SUBSTRING-aware in both
    # directions, after the same normalization `grade_harm`/
    # `grade_correctness` apply (`_normalize_for_match` -- lowercasing
    # only, duplicated here rather than imported for the same
    # avoid-a-circular-import reason `_content_terms` is defined in this
    # module and re-imported into `north_star_report`, not the reverse):
    # a forbidden token that is merely a substring of an answer token (or
    # vice versa) would still make `grade_harm` and `grade_correctness`
    # disagree about the same normalized text, exactly like an exact
    # match would -- checking set membership alone misses that.
    def _normalized(text: str) -> str:
        return text.lower()

    all_answer_tokens: set[str] = {token for p in probes for token in p.answer_tokens}
    normalized_answer_tokens = {_normalized(token): token for token in all_answer_tokens}
    for probe in probes:
        for token in probe.forbidden_tokens:
            owner_uids = sorted({page.uid for page in pages if token in page.body})
            if not owner_uids:
                problems.append(
                    f"probe {probe.id!r}: forbidden_tokens value {token!r} does not occur "
                    "in the body of any corpus page -- it must be plantable"
                )
            elif len(owner_uids) > 1:
                problems.append(
                    f"probe {probe.id!r}: forbidden_tokens value {token!r} occurs on "
                    f"multiple pages {owner_uids} -- must be shared between no two pages"
                )
            for expected_uid in probe.expected_uids:
                expected_page = pages_by_uid.get(expected_uid)
                if expected_page is not None and token in expected_page.body:
                    problems.append(
                        f"probe {probe.id!r}: forbidden_tokens value {token!r} occurs on "
                        f"its own expected_uids page {expected_uid!r} -- it belongs on a "
                        "decoy page, not the correct-answer page"
                    )
            normalized_token = _normalized(token)
            for normalized_answer, answer_token in normalized_answer_tokens.items():
                if normalized_token in normalized_answer or normalized_answer in normalized_token:
                    problems.append(
                        f"probe {probe.id!r}: forbidden_tokens value {token!r} collides "
                        f"(as a normalized substring, either direction) with answer_tokens "
                        f"value {answer_token!r} somewhere in the corpus"
                    )

    # Issue athenaeum#1779: the long-page tier's deterministic honesty check.
    # `recall`'s snippet is windowed to `_snippet`'s `max_chars` default
    # (`src/athenaeum/mcp_server.py`) -- read via `inspect` rather than a
    # second hardcoded `400` so this check cannot silently drift from what
    # `recall` actually shows if that default ever changes. A page in tier
    # `long` must (a) exceed a floor long enough that the tag cannot simply
    # be inside whatever `recall` windows regardless of match position, and
    # (b) place its `Internal reference tag:` line past that offset, so a
    # correct answer is only reachable by `read_entity`, never by `recall`
    # alone. Imported locally, matching this module's existing pattern of
    # local ``athenaeum.mcp_server`` imports confined to the call site that
    # needs them, rather than adding a module-level dependency to a fixture
    # generator that otherwise has none.
    from athenaeum.mcp_server import _snippet as _mcp_snippet

    long_page_tag_offset_floor = inspect.signature(_mcp_snippet).parameters["max_chars"].default
    long_page_min_body_chars = 1500
    long_tier_page_uids = {
        probe_uid
        for probe in probes
        for probe_uid in probe.expected_uids
        if probe_uid in pages_by_uid and pages_by_uid[probe_uid].tier == "long"
    }
    for page_uid in sorted(long_tier_page_uids):
        page = pages_by_uid[page_uid]
        if len(page.body) < long_page_min_body_chars:
            problems.append(
                f"page {page_uid!r}: tier 'long' but body is {len(page.body)} characters, "
                f"below the {long_page_min_body_chars}-character floor"
            )
        tag_match = _TAG_LINE_RE.search(page.body)
        if tag_match is None:
            # Already reported by the missing-tag-line check above; nothing
            # further to say about offset for a page with no tag line at all.
            continue
        if tag_match.start() <= long_page_tag_offset_floor:
            problems.append(
                f"page {page_uid!r}: tier 'long' but 'Internal reference tag:' line begins "
                f"at character offset {tag_match.start()}, not past "
                f"{long_page_tag_offset_floor} (_snippet's max_chars default) -- recall's "
                "own snippet could show the tag without read_entity"
            )

    problems.extend(_validate_relatedness_ground_truth(uids, {p.id for p in probes}))
    return problems


# --------------------------------------------------------------------------
# Relatedness / redundancy ground truth (issue athenaeum#1570)
# --------------------------------------------------------------------------
#
# Held in ``data/corpus/ground_truth/relatedness.yaml`` rather than in the
# pages themselves, and layered onto ``core`` rather than made a fourth tier.
# Both choices follow the tier reasoning in this module's docstring:
#
# * ``core`` is defined as the tier carrying every ground-truth assertion, and
#   these clusters are ground truth. ``distractor`` and ``ballast`` are held
#   apart because each is a GENERATED axis a regression can be attributed to;
#   relatedness is not a generated axis, it is an assertion about specific
#   hand-authored pages. A fourth generated tier would have nothing to
#   generate.
# * The edges live outside the pages because the fixture's whole value is that
#   the edges are ABSENT from the pages. Authoring them into the pages would
#   make every corpus already-correct.


@dataclass(frozen=True)
class FactPlacement:
    """One fact whose correct page differs from where the fixture, as
    committed, puts it (issue athenaeum#1658).

    ``misplaced_on`` names the page the fact is deliberately authored on --
    the live-shape defect athenaeum#1600 measured, a financial fact sitting on
    a person page. ``correct_on`` names the page a librarian that places
    facts correctly would carry it on instead. ``marker`` is the distinctive
    substring :mod:`tests.evals.fact_placement` searches page bodies for --
    offline, no model call, same reasoning as the term-overlap writer this
    fixture also grades.
    """

    fact_id: str
    marker: str
    correct_on: str
    misplaced_on: str
    note: str = ""


@dataclass(frozen=True)
class UnlinkedCluster:
    """Pages that BELONG together and carry no edges between them.

    ``expected_edges`` is the assertion: those edges SHOULD exist and, in the
    fixture as committed, do not. ``fact_placements`` is a SEPARATE
    assertion, orthogonal to edges: it may be empty (``quiet-handover``
    carries none), and a cluster that has one is not thereby exempt from the
    edge assertions above.
    """

    id: str
    members: tuple[str, ...]
    expected_edges: tuple[tuple[str, str, str], ...]  # (source, target, role)
    note: str = ""
    fact_placements: tuple[FactPlacement, ...] = ()


@dataclass(frozen=True)
class RedundantCluster:
    """Pages that are ONE entity written down more than once, plus a control.

    ``merge`` names the pages a consolidation pass should fold together.
    ``negative_control`` names pages that share the cluster's vocabulary and
    must NOT be folded in -- without it, "merge everything sharing a name"
    would score perfectly, which is the redundancy analogue of linking
    everything.

    The merge VERDICT is graded in issue athenaeum#1577, not here. What this
    issue grades is the RETRIEVAL cost of the split, via ``probe``.
    """

    id: str
    merge: tuple[str, ...]
    negative_control: tuple[str, ...]
    probe: str = ""
    note: str = ""


GROUND_TRUTH_DIR = CORPUS_ROOT / "ground_truth"
RELATEDNESS_PATH = GROUND_TRUTH_DIR / "relatedness.yaml"


def load_unlinked_clusters() -> list[UnlinkedCluster]:
    raw = _load_yaml(RELATEDNESS_PATH) or {}
    return [
        UnlinkedCluster(
            id=entry["id"],
            members=tuple(entry["members"]),
            expected_edges=tuple(
                (e["source"], e["target"], e.get("role", "related"))
                for e in entry.get("expected_edges", ())
            ),
            note=entry.get("note", ""),
            fact_placements=tuple(
                FactPlacement(
                    fact_id=f["fact_id"],
                    marker=f["marker"],
                    correct_on=f["correct_on"],
                    misplaced_on=f["misplaced_on"],
                    note=f.get("note", ""),
                )
                for f in entry.get("fact_placements", ())
            ),
        )
        for entry in raw.get("unlinked_clusters", ())
    ]


def load_redundant_clusters() -> list[RedundantCluster]:
    raw = _load_yaml(RELATEDNESS_PATH) or {}
    return [
        RedundantCluster(
            id=entry["id"],
            merge=tuple(entry["merge"]),
            negative_control=tuple(entry.get("negative_control", ())),
            probe=entry.get("probe", ""),
            note=entry.get("note", ""),
        )
        for entry in raw.get("redundant_clusters", ())
    ]


def _validate_relatedness_ground_truth(uids: set[str], probe_ids: set[str]) -> list[str]:
    """Same reasoning as the rest of :func:`validate_core`.

    A ground-truth edge naming a page that does not exist scores as a MISSING
    edge -- indistinguishable from a librarian that failed to write it. A
    corpus error must never be able to masquerade as an eval result.
    """
    problems: list[str] = []
    for unlinked in load_unlinked_clusters():
        for uid in unlinked.members:
            if uid not in uids:
                problems.append(f"unlinked cluster {unlinked.id!r}: unknown member {uid!r}")
        for source, target, _role in unlinked.expected_edges:
            for uid in (source, target):
                if uid not in unlinked.members:
                    problems.append(
                        f"unlinked cluster {unlinked.id!r}: expected edge names {uid!r}, "
                        "which is not a member of the cluster"
                    )
            if source == target:
                problems.append(f"unlinked cluster {unlinked.id!r}: self-edge on {source!r}")
        for placement in unlinked.fact_placements:
            for uid in (placement.correct_on, placement.misplaced_on):
                if uid not in unlinked.members:
                    problems.append(
                        f"unlinked cluster {unlinked.id!r}: fact placement "
                        f"{placement.fact_id!r} names {uid!r}, which is not a "
                        "member of the cluster"
                    )
            if placement.correct_on == placement.misplaced_on:
                problems.append(
                    f"unlinked cluster {unlinked.id!r}: fact placement "
                    f"{placement.fact_id!r} names the same page as both "
                    "correct_on and misplaced_on"
                )
    for redundant in load_redundant_clusters():
        for uid in (*redundant.merge, *redundant.negative_control):
            if uid not in uids:
                problems.append(f"redundant cluster {redundant.id!r}: unknown member {uid!r}")
        overlap = set(redundant.merge) & set(redundant.negative_control)
        if overlap:
            problems.append(
                f"redundant cluster {redundant.id!r}: {sorted(overlap)} is both a merge "
                "target and a negative control -- the control must be a page the merge "
                "must NOT swallow"
            )
        if not redundant.negative_control:
            problems.append(
                f"redundant cluster {redundant.id!r}: no negative control. A redundancy "
                "cluster without one licenses merge-everything."
            )
        if redundant.probe and redundant.probe not in probe_ids:
            problems.append(
                f"redundant cluster {redundant.id!r}: probe {redundant.probe!r} "
                "is not in probes.yaml"
            )
    return problems


# --------------------------------------------------------------------------
# Generation -- the two axes
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Scale:
    """One point in the (size, confusability) plane.

    ``total_pages`` and ``distractors_per_probe`` are INDEPENDENT on purpose.
    Scaling them together is the intuitive thing to do and it destroys the
    result: a corpus whose near-miss count rises with its page count cannot
    say which of the two caused a recall drop, and the two have different
    fixes. Keeping them apart costs grid cells and buys an interpretable
    answer.
    """

    name: str
    total_pages: int
    distractors_per_probe: int

    # ``total_pages`` is a FLOOR that ballast fills to, not a cap. Core and
    # distractor pages are never trimmed to fit: ground truth and retrieval
    # pressure are the measurement, page count is only the padding around
    # them. A scale whose floor is below core+distractors simply produces no
    # ballast -- see ``build_corpus``.


# The grid. Sizes vary down one axis, confusability down the other, so a
# report can hold one constant while moving the other.
SCALES: dict[str, Scale] = {
    # Core only -- no generation. The offline default: fast, fully
    # hand-authored, and what unit/e2e tests run against.
    "core": Scale("core", total_pages=0, distractors_per_probe=0),
    # 300, repinned from 200 (athenaeum#1780, then again by athenaeum#1781
    # item G / Quine review): the hand-authored core has grown across both
    # issues (athenaeum#1780's 14 new pages, this PR's 12 contradiction/
    # negative_knowledge pages, plus each issue's own new distractor pages
    # at 2-per-probe) to the point that core+distractor alone at this scale
    # is 231 pages (measured against the final merged corpus), already
    # exceeding the original 200-page floor and leaving zero ballast --
    # quietly collapsing `small` into a non-distinct point on the size axis
    # (`test_page_floors_leave_room_for_ballast`) -- the same failure mode
    # this floor's own docstring warns about. 300 restores 69 pages of
    # ballast headroom for the merged corpus; re-derive again if a future
    # PR's core/probe growth closes it.
    "small": Scale("small", total_pages=300, distractors_per_probe=2),
    "medium": Scale("medium", total_pages=1_000, distractors_per_probe=2),
    "large": Scale("large", total_pages=10_000, distractors_per_probe=2),
    # A real single-operator deployment is already past 20,000 pages
    # (athenaeum#1735) -- `large` alone cannot show whether the answer holds
    # at the size the project actually runs at. Opt-in at the grid-dispatch
    # level (`tests/evals/north_star_cli.py`'s `DEFAULT_CORPUS_SCALES`
    # deliberately excludes it) because a `NATIVE_GREP` cell over 25k files
    # is the most expensive cell in the grid.
    "xlarge": Scale("xlarge", total_pages=25_000, distractors_per_probe=2),
    # Confusability axis: page count held at `medium` while near-miss density
    # rises. If recall degrades here but not across small->large, the problem
    # is confusability, not scale.
    "medium_dense": Scale("medium_dense", total_pages=1_000, distractors_per_probe=8),
    "medium_verydense": Scale("medium_verydense", total_pages=1_000, distractors_per_probe=24),
}


def _stable_hash(value: str) -> int:
    """Process-stable integer digest of *value*.

    Builtin ``hash()`` is salted per process, so anything derived from it
    varies between runs. Every generation input must be reproducible or the
    corpus fingerprint stops identifying a corpus.
    """
    return int.from_bytes(hashlib.sha256(value.encode()).digest()[:8], "big")


def _render(template: str, slots: dict[str, str]) -> str:
    out = template
    for key, value in slots.items():
        out = out.replace("{" + key + "}", value)
    return out


def _synthetic_name(rng: random.Random, pools: dict[str, list[str]]) -> str:
    """Compose a name from syllable pools rather than sampling a name list.

    Composition (not sampling) is what keeps the pool from ever being a list
    of real people: there is no source list to leak from. Collisions with real
    names remain possible by chance, which is why the PII lint re-checks
    generated output against a runtime denylist instead of trusting this.
    """
    first = rng.choice(pools["given_syllables"]) + rng.choice(pools["given_endings"])
    last = rng.choice(pools["surname_syllables"]) + rng.choice(pools["surname_endings"])
    return f"{first.capitalize()} {last.capitalize()}"


def _generate_distractors(
    probes: list[Probe],
    per_probe: int,
    rng: random.Random,
    templates: dict[str, Any],
) -> list[Page]:
    """Near-miss pages: share a probe's vocabulary, withhold its answer.

    Built from each probe's own ``distractor_terms`` so the competition is
    real. Generic filler would not compete for rank at all, and a corpus that
    cannot crowd out the right answer cannot demonstrate that crowding happens.
    """
    pages: list[Page] = []
    forms = templates["distractor_forms"]
    pools = templates["name_pools"]
    for probe in probes:
        terms = list(probe.distractor_terms) or list(probe.query.split())
        for i in range(per_probe):
            # NOT builtin hash(): Python randomizes string hashing per
            # process unless PYTHONHASHSEED is pinned, which would make the
            # emitted tree differ between runs and silently falsify the
            # (GENERATOR_VERSION, seed, scale) reproducibility contract that
            # every stored measurement cites.
            form = forms[(_stable_hash(probe.id) + i) % len(forms)]
            term = terms[i % len(terms)]
            slots = {
                "term": term,
                "other_term": terms[(i + 1) % len(terms)],
                "person": _synthetic_name(rng, pools),
                "n": str(rng.randint(2, 40)),
                "month": rng.choice(templates["months"]),
            }
            uid = f"dis-{probe.id}-{i:03d}"
            pages.append(
                Page(
                    uid=uid,
                    type=form["type"],
                    name=_render(form["name"], slots),
                    body=_render(form["body"], slots),
                    tier="distractor",
                    # Aliases and tags are rendered with the probe's own terms
                    # rather than left static. The keyword scorer weights
                    # frontmatter (name/aliases/tags) at 3x body text, so a
                    # near-miss carrying the term only in its title cannot
                    # compete with an answer page carrying it in all four --
                    # and a distractor tier that never reaches the top-k
                    # measures nothing, which is how this shipped inert once.
                    aliases=tuple(_render(a, slots) for a in form.get("aliases", ())),
                    tags=tuple(_render(t, slots) for t in form.get("tags", ())),
                    source_ref=f"session-2026-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}",
                )
            )
    return pages


def _generate_ballast(count: int, rng: random.Random, templates: dict[str, Any]) -> list[Page]:
    """Topic-diverse pages that give the index realistic size.

    Deliberately NOT competitive with any probe: ballast measures the effect
    of corpus size alone. If ballast shared probe vocabulary it would be
    distractor mass under another name, and the two axes would be confounded.
    """
    pages: list[Page] = []
    forms = templates["ballast_forms"]
    pools = templates["name_pools"]
    for i in range(count):
        form = forms[i % len(forms)]
        slots = {
            "person": _synthetic_name(rng, pools),
            "topic": rng.choice(templates["ballast_topics"]),
            "other_topic": rng.choice(templates["ballast_topics"]),
            "n": str(rng.randint(2, 90)),
            "month": rng.choice(templates["months"]),
        }
        pages.append(
            Page(
                uid=f"bal-{i:05d}",
                type=form["type"],
                name=_render(form["name"], slots),
                body=_render(form["body"], slots),
                tier="ballast",
                tags=tuple(form.get("tags", ())),
                source_ref=f"session-2026-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}",
            )
        )
    return pages


def build_corpus(scale: str = "core", seed: int = 20260908) -> Corpus:
    """Build a corpus at ``scale``, deterministically from ``seed``.

    The core tier is always present and always identical; only the generated
    tiers vary. That means every scale answers the same probes against the
    same ground truth, which is what makes results comparable ACROSS scales --
    the comparison is the entire point of the size axis.
    """
    if scale not in SCALES:
        raise ValueError(f"unknown scale {scale!r}; known: {sorted(SCALES)}")
    spec = SCALES[scale]
    core = load_core_pages()
    probes = load_probes()

    problems = validate_core(core, probes)
    if problems:
        raise ValueError("hand-authored corpus is inconsistent:\n  " + "\n  ".join(problems))

    rng = random.Random(seed)
    templates = _load_yaml(TEMPLATE_DIR / "generated.yaml")

    pages = list(core)
    if spec.distractors_per_probe:
        pages.extend(_generate_distractors(probes, spec.distractors_per_probe, rng, templates))
    # Ballast fills whatever remains of the size budget. Core and distractors
    # are never trimmed to fit: ground truth and retrieval pressure are the
    # point, page count is the padding.
    remaining = spec.total_pages - len(pages)
    if remaining > 0:
        pages.extend(_generate_ballast(remaining, rng, templates))

    return Corpus(pages=pages, probes=probes, seed=seed, scale=scale)


# --------------------------------------------------------------------------
# Raw-observation generator (issue athenaeum#1726, design lock
# docs/design/native-memory-baseline.md §5 "Phase 2")
# --------------------------------------------------------------------------
#
# Phase 1 (issue athenaeum#1725) hands both systems the same COMPILED pages,
# so it can only measure the READ path. Athenaeum's claimed invention is the
# WRITE path (docs/why-athenaeum.md, "writes are harder than reads"), and a
# read-only comparison cannot see it. This section inverts the hand-authored
# ``core`` pages back into the dated raw observations that would compile
# INTO them -- the same ``source_type``/``source_ref``/``created``/``updated``
# fields ``load_core_pages`` already reads off every page (see
# :class:`Page`) make this a generator change, never a corpus rewrite: no
# file under ``data/corpus/core/`` is touched by anything below.
#
# The observations are emitted in the RAW-INTAKE shape
# (``src/athenaeum/intake.py``'s ``RawFile``/``RAW_FILE_RE``: a bare-body
# file named ``{timestamp}-{uuid8}.md`` under ``raw/<source>/``) rather than
# a pre-structured wiki page. That choice is load-bearing: a pre-structured
# raw (frontmatter carrying ``uid``/``type``/``name``) is eligible for
# ``athenaeum.intake.tier0_passthrough``, which writes it to the wiki
# byte-for-byte with NO LLM call at all -- exactly the write decision Phase 2
# exists to measure would never run. See
# ``tests/evals/test_raw_observation_roundtrip.py`` for the proof that this
# shape reaches the ordinary Tier 1/2/3 chain.


def _page_answer_tokens(page: "Page", probes: list["Probe"]) -> tuple[str, ...]:
    """Every ``answer_tokens`` value any probe plants on *page*, in probe order.

    A probe's ``answer_tokens`` is validated (:func:`validate_core`) to occur
    in the body of at least one of its ``expected_uids`` pages -- but not
    necessarily THIS one, for a ``multi_hop``/``disambiguation`` probe naming
    several pages. Membership is decided the same way ``validate_core``
    decides it: a literal substring check against *this* page's own body,
    never against the probe's other expected pages.
    """
    tokens: list[str] = []
    for probe in probes:
        if page.uid not in probe.expected_uids:
            continue
        for token in probe.answer_tokens:
            if token in page.body and token not in tokens:
                tokens.append(token)
    return tuple(tokens)


def _observation_paragraphs(body: str) -> list[str]:
    """Split a page body into observation-sized slices.

    Every authored ``body:`` in ``data/corpus/core/*.yaml`` opens with a
    markdown ``# <name>`` heading (:func:`Page.to_markdown` renders it
    verbatim -- see ``tests/evals/rollout.py``'s ``_native_index_description``
    for the same observation about this corpus's authoring convention), and a
    bare heading carries no observable fact -- it is dropped here rather than
    emitted as a content-free "observation". Splits on the blank line the
    YAML ``body: |`` block literal already uses to separate paragraphs, so no
    planted token is ever cut in half: a token is authored as a whole word
    inside one paragraph, never spanning the blank-line boundary.

    Falls back to the whole (heading-stripped) body as a single slice if
    every paragraph turned out to be a heading -- a defensive floor, not a
    case any committed core page hits today.
    """
    paragraphs = [p.strip() for p in body.strip().split("\n\n") if p.strip()]
    kept = [p for p in paragraphs if not p.startswith("#")]
    if kept:
        return kept
    whole = "\n\n".join(p for p in paragraphs if p) or body.strip()
    return [whole] if whole else []


@dataclass(frozen=True)
class Observation:
    """One raw-intake observation -- the write-path input the librarian's
    Tier 1/2/3 chain (``athenaeum.intake``/``athenaeum.tiers``) actually
    consumes, per issue athenaeum#1726 AC1/AC2.

    ``page_uid`` and ``answer_tokens`` are GENERATOR GROUND TRUTH, carried as
    DATA rather than left for a grader to re-derive later by string matching
    (the issue's own requirement): which compiled page this observation is
    ABOUT, and which of that page's planted :attr:`Probe.answer_tokens` this
    specific slice carries (empty when this slice carries none -- most
    slices of a page do not carry the one planted marker token).

    ``body`` is the bare raw-intake text -- no frontmatter -- prefixed with
    the subject page's own ``name`` (e.g. ``"PTO policy: The firm's PTO
    allowance is 25 days..."``). That prefix is what lets
    ``athenaeum.tiers.tier1_programmatic_match`` attribute a later
    observation to the page deterministically once it exists in the wiki
    index, the same way a real observation naturally names its subject --
    without it, only the FIRST observation about a page (which mints it)
    would ever land; every later slice would silently have no name or alias
    for Tier 1 to match and would be dropped as "no actions needed" before
    a single token could compile through.
    """

    uid: str
    page_uid: str
    source: str
    timestamp: str
    uuid8: str
    body: str
    answer_tokens: tuple[str, ...] = ()

    @property
    def filename(self) -> str:
        """``{timestamp}-{uuid8}.md`` -- matches
        ``athenaeum.intake.RAW_FILE_RE`` exactly, so a materialized
        observation is discoverable by
        :func:`athenaeum.intake.discover_raw_files` like any real raw file."""
        return f"{self.timestamp}-{self.uuid8}.md"


def generate_page_observations(
    page: Page,
    probes: list[Probe],
    seed: int = 20260908,
    scale: str = "core",
) -> list[Observation]:
    """Invert one core *page* into the dated observations that would compile
    back into it (design doc §5, Phase 2).

    Deterministic for ``(GENERATOR_VERSION, seed, page.uid)`` -- same
    discipline as :func:`build_corpus`'s own generation: every derived value
    comes from :func:`_stable_hash` seeding a ``random.Random``, never the
    salted builtin ``hash()``. Two calls with identical inputs return
    byte-identical output (see
    ``tests/evals/test_eval_corpus_observation_generator.py``'s determinism
    coverage).

    *scale* is accepted and VALIDATED against :data:`SCALES` -- an unknown
    value raises, exactly like :func:`build_corpus` -- but never consulted
    for anything else. This is deliberate, and STRONGER than AC1's own
    "deterministic for ``(GENERATOR_VERSION, seed, scale)``" requirement,
    not a gap in it: only the hand-authored ``core`` tier carries the
    ground truth (``page`` itself, and the ``answer_tokens`` planted on it)
    a write-path measurement needs -- ``distractor``/``ballast`` pages carry
    none (see :func:`generate_core_observations`'s own note). Output that
    does not depend on *scale* at all is trivially deterministic for every
    fixed value of it, including two different ones compared against each
    other -- see ``test_generate_page_observations_is_invariant_across_scale``.

    Each of *page*'s planted ``answer_tokens`` (:func:`_page_answer_tokens`)
    is distributed onto whichever of its own generated observations actually
    contains that token's text -- so a caller that drops an observation can
    measure exactly which tokens went missing, rather than losing the fact
    silently. Dated starting at ``page.created``, one day apart per
    observation, in source order (the seeded RNG is reserved for the
    corpus-wide interleave in :func:`generate_core_observations`; a single
    page's own slices need no shuffling to be a faithful inversion).
    """
    if scale not in SCALES:
        raise ValueError(f"unknown scale {scale!r}; known: {sorted(SCALES)}")
    tokens = _page_answer_tokens(page, probes)
    paragraphs = _observation_paragraphs(page.body)
    # ``page.created`` is typed ``str`` (:class:`Page`), but PyYAML resolves
    # an UNQUOTED ``created: 2026-05-14`` scalar (every core page's actual
    # authoring style -- see ``data/corpus/core/*.yaml``) to a real
    # ``datetime.date`` object, not a string; ``load_core_pages`` passes it
    # through unconverted. Accept either representation rather than assuming
    # the annotation matches the runtime value.
    created = page.created if isinstance(page.created, date) else date.fromisoformat(page.created)
    observations: list[Observation] = []
    for i, paragraph in enumerate(paragraphs):
        obs_tokens = tuple(t for t in tokens if t in paragraph)
        obs_uid = f"obs-{page.uid}-{i:03d}"
        obs_date = created + timedelta(days=i)
        digest = hashlib.sha256(f"v{GENERATOR_VERSION}:{seed}:{obs_uid}".encode()).hexdigest()
        observations.append(
            Observation(
                uid=obs_uid,
                page_uid=page.uid,
                source="sessions",
                timestamp=obs_date.strftime("%Y%m%dT%H%M%SZ"),
                uuid8=digest[:8],
                body=f"{page.name}: {paragraph}",
                answer_tokens=obs_tokens,
            )
        )
    return observations


@dataclass
class ObservationStream:
    """A materialized observation stream: the write-path counterpart to
    :class:`Corpus` -- observations plus the seed and scale that produced
    them.

    ``scale`` mirrors :attr:`Corpus.scale` for symmetry and provenance
    (a stored measurement should be able to name the scale a stream was
    generated at, the same way :meth:`Corpus.fingerprint` lets one name a
    corpus), but the stream's CONTENT is deliberately INVARIANT across it --
    see :func:`generate_core_observations`'s docstring for why that is
    strictly stronger than AC1's determinism requirement, not a gap in it.
    """

    observations: list[Observation] = field(default_factory=list)
    seed: int = 0
    scale: str = "core"

    def materialize(self, root: Path) -> Path:
        """Write every observation to ``root/raw/<source>/<filename>`` --
        the raw-intake layout ``athenaeum.intake.discover_raw_files`` walks.
        Writes only under *root*, mirroring :meth:`Corpus.materialize`'s own
        discipline (enforced the same way, in
        ``tests/test_eval_corpus_leakage.py``)."""
        raw_root = root / "raw"
        for obs in self.observations:
            source_dir = raw_root / obs.source
            source_dir.mkdir(parents=True, exist_ok=True)
            (source_dir / obs.filename).write_text(obs.body, encoding="utf-8")
        return raw_root

    def answer_tokens(self) -> frozenset[str]:
        """Every distinct planted token carried anywhere in the stream --
        the denominator a "tokens retained" measurement (issue athenaeum#1726
        AC4) is computed against."""
        return frozenset(t for obs in self.observations for t in obs.answer_tokens)


def generate_core_observations(
    pages: list[Page] | None = None,
    probes: list[Probe] | None = None,
    seed: int = 20260908,
    scale: str = "core",
) -> ObservationStream:
    """Invert every hand-authored core page into the interleaved observation
    stream Phase 2 feeds identically to both systems (design doc §5).

    Deterministic for ``(GENERATOR_VERSION, seed, scale)`` per AC1, like
    :func:`build_corpus` -- ``scale`` is VALIDATED against :data:`SCALES`
    (an unknown value raises the same ``ValueError`` shape
    :func:`build_corpus` raises) but otherwise never consulted, and the
    returned stream is INVARIANT across every valid scale. That is
    deliberate and strictly STRONGER than the AC's own wording asks for, not
    a gap in it: ``pages``/``probes`` default to
    :func:`load_core_pages`/:func:`load_probes`, and only the hand-authored
    ``core`` tier is ever inverted (see this section's module-level note) --
    the generated ``distractor``/``ballast`` tiers :data:`Scale` controls
    carry no ground truth (no ``answer_tokens``, no ``page_uid`` a probe
    targets) for a write-path measurement to check, so there is nothing
    about a larger scale for this generator to reflect. A caller that wants
    read-side pressure alongside the observation stream still gets it from
    :meth:`Corpus.materialize` at whatever scale it likes -- that tree and
    this stream are generated, and validated, independently.
    ``test_generate_core_observations_is_invariant_across_scale`` pins this
    as a contract: the SAME seed at two different scales returns identical
    observations (order included), and an unknown scale raises.

    The per-page slices from :func:`generate_page_observations` are then
    shuffled with a seeded ``random.Random`` -- deterministic for
    ``(GENERATOR_VERSION, seed)``, but no longer grouped page-by-page. Real
    observations about different entities arrive interleaved (a session
    transcript mentions several people and policies in whatever order they
    came up), not one entity fully narrated before the next begins; an
    ungrouped stream is the honest shape for both a librarian compile run
    and a sequence of native-writer sessions to consume.
    """
    if scale not in SCALES:
        raise ValueError(f"unknown scale {scale!r}; known: {sorted(SCALES)}")
    resolved_pages = pages if pages is not None else load_core_pages()
    resolved_probes = probes if probes is not None else load_probes()
    observations: list[Observation] = []
    for page in resolved_pages:
        observations.extend(generate_page_observations(page, resolved_probes, seed=seed))
    rng = random.Random(_stable_hash(f"v{GENERATOR_VERSION}:{seed}:observation-interleave"))
    rng.shuffle(observations)
    return ObservationStream(observations=observations, seed=seed, scale=scale)
