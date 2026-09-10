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
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

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
        return "\n".join(fm) + "\n\n" + self.body.rstrip() + "\n"

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


@dataclass(frozen=True)
class Probe:
    """A retrieval probe with its ground truth.

    ``probe_class`` follows the LongMemEval-style taxonomy: single_hop,
    multi_hop, temporal, disambiguation, abstention, distractor_robustness.

    ``expected_uids`` is empty for abstention probes -- and that emptiness is
    the assertion, not a missing value. ``must_not_rank`` names pages that a
    correct system keeps OUT of the top-k, which is how a disambiguation
    probe states the failure it is guarding against.
    """

    id: str
    probe_class: str
    query: str
    expected_uids: tuple[str, ...] = ()
    must_not_rank: tuple[str, ...] = ()
    distractor_terms: tuple[str, ...] = ()
    note: str = ""


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
                    tier="core",
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
            note=raw.get("note", ""),
        )
        for raw in _load_yaml(PROBES_PATH)
    ]


def validate_core(pages: list[Page], probes: list[Probe]) -> list[str]:
    """Return human-readable problems with the hand-authored corpus.

    Checked here rather than at use time because a probe whose ``expected_uids``
    names a page that does not exist does not fail loudly -- it silently scores
    as a retrieval MISS, which reads as a model regression. A dangling
    ground-truth reference must be a corpus error, never an eval result.
    """
    uids = {p.uid for p in pages}
    problems: list[str] = []
    for probe in probes:
        for uid in probe.expected_uids:
            if uid not in uids:
                problems.append(f"probe {probe.id!r}: expected_uids names unknown page {uid!r}")
        for uid in probe.must_not_rank:
            if uid not in uids:
                problems.append(f"probe {probe.id!r}: must_not_rank names unknown page {uid!r}")
        if probe.probe_class == "abstention" and probe.expected_uids:
            problems.append(f"probe {probe.id!r}: abstention probes must have no expected_uids")
        if probe.probe_class != "abstention" and not probe.expected_uids:
            problems.append(f"probe {probe.id!r}: no expected_uids and not abstention")
    for page in pages:
        for edge in page.related:
            if edge.uid not in uids:
                problems.append(f"page {page.uid!r}: related edge names unknown page {edge.uid!r}")
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
class UnlinkedCluster:
    """Pages that BELONG together and carry no edges between them.

    ``expected_edges`` is the assertion: those edges SHOULD exist and, in the
    fixture as committed, do not.
    """

    id: str
    members: tuple[str, ...]
    expected_edges: tuple[tuple[str, str, str], ...]  # (source, target, role)
    note: str = ""


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
    # 200 rather than 100: the hand-authored core is ~90 pages and the
    # distractor tier adds ~40 on top, so a 100-page floor would produce zero
    # ballast and quietly stop being a distinct point on the size axis.
    "small": Scale("small", total_pages=200, distractors_per_probe=2),
    "medium": Scale("medium", total_pages=1_000, distractors_per_probe=2),
    "large": Scale("large", total_pages=10_000, distractors_per_probe=2),
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
