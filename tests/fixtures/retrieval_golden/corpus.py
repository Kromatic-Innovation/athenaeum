# SPDX-License-Identifier: Apache-2.0
"""Deterministic fixture corpus for the retrieval golden-set suite (athenaeum#1420).

Builds a small synthetic wiki with a realistic tier/type mix (AC2): a small
hot minority confined to ``principle``/``preference``/``auto-memory`` pages,
and a dominant warm majority spanning many other entity types — proportioned
after the real-corpus measurement in athenaeum#1420's finding (23,768 warm /
848 hot of 24,616 pages = 96.56% / 3.44%). An all-hot or all-warm fixture
cannot express the substitution bug the issue is about (a hot page silently
backfilling for a suppressed warm one), so this module exists specifically to
avoid that degenerate shape.

A handful of "signal" pages carry vocabulary distinctive enough to be an
unambiguous top hit for a fixed query (see ``QUERIES``) under BOTH a lexical
backend (FTS5/BM25) and the deterministic hashed-bag-of-words vector stand-in
every non-``embedding``-marked test runs under
(``tests/offline_embeddings.py``, wired in via ``tests/conftest.py``'s
autouse ``_offline_embedding_function`` fixture) — so the SAME corpus can
freeze one golden expectation per (backend, query) pair.

Regeneration: see ``update_goldens.py`` in this directory.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PageSpec:
    filename: str
    type: str
    name: str
    tags: list[str]
    description: str
    body: str
    memory_tier: str | None = None  # explicit pin; None = resolve_tier's class default


def _frontmatter(spec: PageSpec) -> str:
    tags_yaml = "[" + ", ".join(spec.tags) + "]"
    lines = [
        "---",
        f"name: {spec.name}",
        f"type: {spec.type}",
        f"tags: {tags_yaml}",
        f"description: {spec.description}",
    ]
    if spec.memory_tier is not None:
        lines.append(f"memory_tier: {spec.memory_tier}")
    lines.append("---")
    return "\n".join(lines) + "\n\n" + spec.body.strip() + "\n"


# ---------------------------------------------------------------------------
# Signal pages -- distinctive vocabulary, each the intended top hit for one
# entry in QUERIES below. All resolve to the WARM tier by class default (no
# memory_tier: override) -- see memory_class.TYPE_TO_MEMORY_CLASS /
# memory_tiers.DEFAULT_TIER_BY_MEMORY_CLASS.
# ---------------------------------------------------------------------------

SIGNAL_PAGES: list[PageSpec] = [
    PageSpec(
        filename="lean-startup-method.md",
        type="concept",
        name="Lean Startup Method",
        tags=["methodology", "startup"],
        description="Build-measure-learn methodology for validated learning",
        body=(
            "The Lean Startup method drives validated learning through a "
            "build-measure-learn feedback loop, minimizing wasted engineering "
            "effort on features nobody wants."
        ),
    ),
    PageSpec(
        filename="customer-development-process.md",
        type="procedure",
        name="Customer Development Process",
        tags=["methodology", "customers"],
        description="Steve Blank's four-step customer development framework",
        body=(
            "Customer development is a four-step process -- customer "
            "discovery, customer validation, customer creation, company "
            "building -- popularized by Steve Blank."
        ),
    ),
    PageSpec(
        filename="acme-corp-vendor.md",
        type="company",
        name="Acme Corp",
        tags=["vendor", "fintech"],
        description="Fintech vendor providing invoice financing",
        body=(
            "Acme Corp is a fintech vendor specializing in invoice financing "
            "for small businesses."
        ),
    ),
    PageSpec(
        filename="jane-doe-founder.md",
        type="person",
        name="Jane Doe",
        tags=["founder", "biography"],
        description="Founder and CEO biography",
        body=(
            "Jane Doe is the founder and CEO of Acme Corp, previously an "
            "engineering lead at a payments startup."
        ),
    ),
]

# ---------------------------------------------------------------------------
# Hot pages -- confined to principle/preference/auto-memory (AC2). The three
# `principle` pages are naturally hot via the class default
# (type -> memory_class "guideline" -> DEFAULT_TIER_BY_MEMORY_CLASS["guideline"]
# == "hot"); preference/auto-memory have no class-default mapping at all
# (memory_class.TYPE_TO_MEMORY_CLASS deliberately omits them -- see that
# module's docstring) so they need an explicit memory_tier: hot pin, exactly
# the "human pin" resolution branch resolve_tier documents.
# ---------------------------------------------------------------------------

HOT_PRINCIPLE_PAGES: list[PageSpec] = [
    PageSpec(
        filename="principle-iterate-fast.md",
        type="principle",
        name="Iterate Fast",
        tags=["principle", "process"],
        description="Ship weekly and iterate on a tight build-measure-learn loop",
        body=(
            "Iterate fast: ship weekly and keep the build-measure-learn loop "
            "tight, so a bad bet costs days, not quarters. This principle "
            "applies to every team."
        ),
    ),
    PageSpec(
        filename="principle-fail-fast.md",
        type="principle",
        name="Fail Fast",
        tags=["principle", "process"],
        description="Surface a bad bet early rather than late",
        body=(
            "Fail fast: a cheap, early, well-instrumented failure beats an "
            "expensive, late, undiagnosed one. This principle guides "
            "incident response too."
        ),
    ),
    PageSpec(
        filename="principle-customer-obsession.md",
        type="principle",
        name="Customer Obsession",
        tags=["principle", "customers"],
        description="Work backward from the customer, not the roadmap",
        body=(
            "Customer obsession: work backward from the customer's problem, "
            "not forward from whatever the roadmap already commits to. This "
            "principle overrides sunk-cost planning."
        ),
    ),
]

HOT_PINNED_PAGES: list[PageSpec] = [
    PageSpec(
        filename="preference-terse-writing.md",
        type="preference",
        name="Terse Writing Preference",
        tags=["preference", "style"],
        description="Prefer terse prose; cut filler words on sight",
        body=(
            "Preference: terse writing. Cut filler words on sight; say what "
            "needs saying and no more."
        ),
        memory_tier="hot",
    ),
    PageSpec(
        filename="auto-memory-standup-digest.md",
        type="auto-memory",
        name="Standup Digest",
        tags=["auto-memory", "status"],
        description="Daily standup digest and quarterly summary notes",
        body=(
            "Daily standup digest: yesterday's blockers, today's plan, and "
            "the quarterly summary notes rolled up from the last four weeks."
        ),
        memory_tier="hot",
    ),
]

#: Warm entity types -- all resolve WARM via memory_class_for_type /
#: DEFAULT_TIER_BY_MEMORY_CLASS with no frontmatter override needed.
#: Deliberately excludes axiom/guideline/decision/principle (all default
#: HOT) so filler pages can never accidentally inflate the hot count.
_WARM_FILLER_TYPES = [
    "fact",
    "reference",
    "entity",
    "procedure",
    "person",
    "company",
    "concept",
    "tool",
    "project",
    "source",
]

#: Filler topic nouns -- combined with an index and a type to build a large,
#: lexically-disjoint-from-the-signal-queries warm majority (AC2). Content is
#: deliberately generic/short: these pages exist to establish the ~93-97%
#: warm majority, not to be retrieval targets themselves.
_FILLER_NOUNS = [
    "Widget",
    "Gadget",
    "Vendor",
    "Ledger",
    "Playbook",
    "Runbook",
    "Dashboard",
    "Contract",
    "Roadmap",
    "Backlog",
    "Sprint",
    "Release",
    "Server",
    "Pipeline",
    "Dataset",
    "Metric",
    "Survey",
    "Template",
    "Workflow",
    "Directory",
]


def _filler_pages(count: int) -> list[PageSpec]:
    """Deterministically generate *count* warm filler pages.

    Cycles ``_WARM_FILLER_TYPES`` x ``_FILLER_NOUNS`` so every page's
    ``type:`` resolves warm by class default and no two filler pages
    collide on filename.
    """
    pages: list[PageSpec] = []
    for i in range(count):
        page_type = _WARM_FILLER_TYPES[i % len(_WARM_FILLER_TYPES)]
        noun = _FILLER_NOUNS[i % len(_FILLER_NOUNS)]
        n = i // len(_FILLER_NOUNS)
        slug = f"{page_type}-{noun.lower()}-{n:02d}"
        pages.append(
            PageSpec(
                filename=f"{slug}.md",
                type=page_type,
                name=f"{noun} {n:02d}",
                tags=[page_type, "filler"],
                description=f"Filler {page_type} record #{i} for corpus bulk",
                body=(
                    f"This is filler record number {i}, a {page_type}-class "
                    f"entry named {noun} {n:02d}. It exists to give the "
                    f"fixture corpus a realistic warm-tier majority and "
                    f"carries no distinctive vocabulary of its own."
                ),
            )
        )
    return pages


#: Total filler count. With 4 signal + 3 hot-principle + 2 hot-pinned = 9
#: named pages, 66 filler pages brings the corpus to 75 pages total at a
#: 5:75 = 6.7% hot / 93.3% warm split -- a small hot minority confined to
#: principle/preference/auto-memory, dominant warm majority, matching the
#: SHAPE (not the exact percentage) of athenaeum#1420's real-corpus
#: measurement (96.56% warm / 3.44% hot).
_FILLER_COUNT = 66


def all_pages() -> list[PageSpec]:
    """Every page in the fixture corpus, in a fixed, deterministic order."""
    return [
        *SIGNAL_PAGES,
        *HOT_PRINCIPLE_PAGES,
        *HOT_PINNED_PAGES,
        *_filler_pages(_FILLER_COUNT),
    ]


def build_corpus(wiki_root: Path) -> list[PageSpec]:
    """Write :func:`all_pages` to *wiki_root* as wiki markdown files.

    Idempotent and deterministic: the same *wiki_root* always ends up with
    the same file set and content, which is what makes the golden hits in
    ``golden_hits.json`` reproducible via ``update_goldens.py``.
    """
    wiki_root.mkdir(parents=True, exist_ok=True)
    pages = all_pages()
    for spec in pages:
        (wiki_root / spec.filename).write_text(_frontmatter(spec), encoding="utf-8")
    return pages


#: Fixed query set the golden test suite freezes hit identity+rank against
#: (AC1). Each query is built from a signal page's own distinctive
#: vocabulary so its intended target is an unambiguous top hit for both a
#: lexical (FTS5 BM25) and a hashed-bag-of-words (offline vector stand-in)
#: backend.
QUERIES: list[str] = [
    "lean startup build measure learn methodology",
    "customer development four step process Steve Blank",
    "Acme Corp fintech vendor invoice financing",
    "Jane Doe founder biography",
    "iterate fast ship weekly build measure learn loop",
    "daily standup digest quarterly summary",
]

#: The ``type:`` value shared by every page in HOT_PRINCIPLE_PAGES -- the
#: value the backend-parity ``type_filter`` test (AC3) filters on.
PRINCIPLE_TYPE = "principle"
PRINCIPLE_FILENAMES = frozenset(p.filename for p in HOT_PRINCIPLE_PAGES)

#: Every HOT_PRINCIPLE_PAGES body shares this token verbatim ("principle"),
#: used together with type_filter="principle" to fetch exactly that set from
#: both backends (AC3).
PRINCIPLE_QUERY = "principle"

#: A query built from the parity-target page's own vocabulary (AC4): a
#: single, lexically unambiguous top hit both backends should agree on.
PARITY_QUERY = "lean startup build measure learn methodology"
PARITY_FILENAME = "lean-startup-method.md"
