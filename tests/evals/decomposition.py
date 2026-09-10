# SPDX-License-Identifier: Apache-2.0
"""Offline decomposition measure for the synthetic corpus (issue athenaeum#1581).

Grades one question: when a page has grown to cover several FACETS, does the
librarian decompose it into a hub plus linked facet sub-pages -- and does it
leave alone a page that is merely LONG?

What is graded, and against which tier
--------------------------------------

The shipped librarian has exactly one code path that decomposes a page:
:func:`athenaeum.tiers.check_page_size_gate`, whose ``split`` disposition
calls :func:`athenaeum.tiers._perform_oversize_page_split`. It is reached
through that public gate here, never by calling the private split directly,
so what this module grades is what production runs. Its trigger is
``len(existing_body) > resolve_page_size_threshold_chars(config)`` -- SIZE,
with no notion of a facet anywhere in it. That is the gap this layer exists
to measure, so the measure must be able to say *which tier decided*
(athenaeum#1581 AC2) and not merely *what happened*.

Nothing is ever applied to a durable corpus
-------------------------------------------

Two separate readings, and both are taken (athenaeum#1581 AC4,
``docs/north-star.md`` §2.7-2.8):

* At the SHIPPED DEFAULT (``oversize_page_action: review``) the gate returns
  an :class:`~athenaeum.models.EscalationItem` and leaves the page
  byte-for-byte unmodified. :func:`observe_default_disposition` asserts that
  reading: the proposal reaches the queue, the page on disk does not move.
* At ``oversize_page_action: split`` the gate enacts the restructure, which
  is the only way to see WHAT a decomposition proposal would contain. That
  run happens in a caller-supplied ``tmp_path`` wiki materialized for the
  case and thrown away after it -- :func:`assert_disposable` refuses to run
  against anything under a real knowledge tree.

Facet alignment, not section count
----------------------------------

"Did it split?" is not the measurement. ``_split_into_atomic_sections`` cuts
at the shallowest markdown heading depth present, which is a TYPOGRAPHIC
boundary; the fixture's ground truth is a FACET boundary. They coincide only
when a page's headings happen to be its facets. :func:`facet_alignment`
therefore scores which ground-truth fact keys landed on which child page,
and penalises a child carrying facts from two facets exactly as it penalises
a facet scattered across two children.

Edge roles are graded BY NAME
-----------------------------

Since athenaeum#1576 the compile-time relatedness writer stamps ``related:``
rows with ``role: term-overlap`` on ordinary pages. A hub and its facet
sub-pages share almost all of their vocabulary, so they are precisely the
pages that writer would link anyway. An assertion that "an edge exists
between the hub and the sub-page" would therefore pass on a corpus where no
split ever happened. :data:`SPLIT_ROLES` names the two roles the split
disposition itself writes -- ``split-into`` on the hub, ``split-from`` on
each child -- and :func:`split_edges` counts only those.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from athenaeum.models import EntityAction, EscalationItem, parse_frontmatter, render_frontmatter
from athenaeum.name_structure import merged_body_within_page_size_threshold
from athenaeum.tiers import check_page_size_gate, resolve_page_size_threshold_chars

CASES_PATH = Path(__file__).parent / "data" / "decomposition" / "cases.yaml"

#: The two ``related:`` roles the oversize split disposition writes
#: (``tiers._build_split_child_pages``). Graded by name -- see the module
#: docstring for why "an edge exists" would grade athenaeum#1576's writer
#: instead of this one.
ROLE_SPLIT_FROM = "split-from"
ROLE_SPLIT_INTO = "split-into"
SPLIT_ROLES = frozenset({ROLE_SPLIT_FROM, ROLE_SPLIT_INTO})

#: Deciding-tier labels reported per case (athenaeum#1581 AC2).
TIER_SIZE_GATE = "deterministic_size_gate"
TIER_CLASSIFY = "classify"

#: One line of declared, fact-free padding. See ``cases.yaml`` for why the
#: fixture pads at all: the cases have to sit above a 10,000-character
#: threshold, and ten thousand characters of invented prose per case would
#: stop the fixture being auditable by reading it. Carries no name and no
#: number that any ground-truth fact key could match, so padding can never
#: satisfy an assertion.
_PAD_LINE = "This paragraph continues the same account and records no further detail."


def contains_fact(body: str, key: str) -> bool:
    """Is *key* present in *body*, ignoring how the prose happens to wrap?

    Fact keys are short phrases (``"88 thousand GBP"``) and fixture bodies are
    hand-wrapped prose, so a naive ``in`` test fails whenever a key straddles
    a line break -- silently, and as a MISSING FACT, which reads as the
    librarian having dropped it. Both sides collapse whitespace runs to a
    single space first, so the measure grades content rather than the
    fixture's line width.
    """
    return " ".join(key.split()) in " ".join(body.split())


def assert_disposable(wiki_root: Path) -> None:
    """Refuse to run against anything that could be a real knowledge tree.

    The same guard ``tests/test_eval_corpus_consolidation.py`` applies, for
    the same reason: this module's ``split`` replay WRITES pages, and a path
    that resolved somewhere durable would restructure an operator's corpus
    to take a measurement.
    """
    resolved = wiki_root.resolve()
    home_knowledge = (Path.home() / "knowledge").resolve()
    if resolved == home_knowledge or home_knowledge in resolved.parents:
        raise AssertionError(f"refusing to run a decomposition replay under {home_knowledge}")


# ---------------------------------------------------------------------------
# Fixture loading and rendering
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Section:
    """One authored section of a fixture page, with its declared size."""

    facet: str
    heading: str
    text: str
    pad_to_chars: int = 0

    def render(self) -> str:
        """Heading (when present) plus text, padded to :attr:`pad_to_chars`.

        Padding is appended as whole :data:`_PAD_LINE` lines, so a section's
        rendered length is deterministic for a given target but not exactly
        equal to it. Overshoot is fine and undershoot is not: the assertion
        the fixture cares about is "above the threshold", so the loop runs
        until the target is reached or passed.
        """
        parts: list[str] = []
        if self.heading:
            parts.append(f"# {self.heading}")
        parts.append(self.text.strip())
        rendered = "\n\n".join(parts)
        while len(rendered) < self.pad_to_chars:
            rendered += "\n\n" + _PAD_LINE
        return rendered


@dataclass(frozen=True)
class PageSpec:
    """A fixture page: frontmatter fields plus its authored sections."""

    uid: str
    type: str
    name: str
    access: str
    tags: tuple[str, ...]
    sections: tuple[Section, ...]
    related: tuple[tuple[str, str], ...] = ()

    def body(self) -> str:
        return "\n\n".join(section.render() for section in self.sections) + "\n"

    def meta(self) -> dict[str, Any]:
        meta: dict[str, Any] = {
            "uid": self.uid,
            "type": self.type,
            "name": self.name,
            "access": self.access,
            "tags": list(self.tags),
            "created": "2026-01-01",
            "updated": "2026-01-01",
        }
        if self.related:
            meta["related"] = [{"uid": uid, "role": role} for uid, role in self.related]
        return meta

    def render(self) -> str:
        return render_frontmatter(self.meta()) + "\n" + self.body()

    def write(self, wiki_root: Path) -> Path:
        path = wiki_root / f"{self.uid}.md"
        path.write_text(self.render(), encoding="utf-8")
        return path


@dataclass(frozen=True)
class Case:
    """One decomposition case with its ground truth."""

    id: str
    expected_verdict: str
    deciding_tier: str
    page: PageSpec
    intake_facet: str
    intake_text: str
    facets: dict[str, tuple[str, ...]]
    subpages: tuple[PageSpec, ...] = ()
    expected_target_uid: str = ""

    @property
    def expects_decompose(self) -> bool:
        return self.expected_verdict == "decompose"


def _section(raw: dict[str, Any]) -> Section:
    return Section(
        facet=str(raw["facet"]),
        heading=str(raw.get("heading", "")),
        text=str(raw["text"]),
        pad_to_chars=int(raw.get("pad_to_chars", 0)),
    )


def _page(raw: dict[str, Any], *, related: tuple[tuple[str, str], ...] = ()) -> PageSpec:
    return PageSpec(
        uid=str(raw["uid"]),
        type=str(raw["type"]),
        name=str(raw["name"]),
        access=str(raw.get("access", "internal")),
        tags=tuple(raw.get("tags", ())),
        sections=tuple(_section(s) for s in raw["sections"]),
        related=related,
    )


def _raw_fixture() -> dict[str, Any]:
    return yaml.safe_load(CASES_PATH.read_text(encoding="utf-8"))


def load_cases() -> list[Case]:
    """Every case in ``data/decomposition/cases.yaml``, in file order.

    The trailing ``oversize_family:`` mapping is NOT a case -- it is the
    boundary demonstration, loaded separately by :func:`load_oversize_family`.
    ``yaml.safe_load`` returns a list for the document, so the family is read
    from its own second document rather than filtered out of this one.
    """
    raw = _raw_fixture()
    cases: list[Case] = []
    for entry in raw["cases"]:
        hub_uid = str(entry["page"]["uid"])
        subpages = tuple(
            PageSpec(
                uid=str(sub["uid"]),
                type=str(entry["page"]["type"]),
                name=str(sub["name"]),
                access=str(entry["page"].get("access", "internal")),
                tags=tuple(entry["page"].get("tags", ())),
                sections=(Section(facet=str(sub["facet"]), heading="", text=str(sub["text"])),),
                related=((hub_uid, ROLE_SPLIT_FROM),),
            )
            for sub in entry.get("existing_subpages", ())
        )
        hub_related = tuple((sub.uid, ROLE_SPLIT_INTO) for sub in subpages)
        cases.append(
            Case(
                id=str(entry["id"]),
                expected_verdict=str(entry["expected_verdict"]),
                deciding_tier=str(entry["deciding_tier"]),
                page=_page(entry["page"], related=hub_related),
                intake_facet=str(entry["intake"]["facet"]),
                intake_text=str(entry["intake"]["text"]),
                facets={k: tuple(v) for k, v in entry["facets"].items()},
                subpages=subpages,
                expected_target_uid=str(entry.get("expected_target_uid", "")),
            )
        )
    return cases


def load_oversize_family() -> tuple[PageSpec, PageSpec]:
    """The ``bare`` / ``bare (qualifier)`` pair that must NOT be merged.

    The counterpart to athenaeum#1570 Cluster B's short pair, and the reason
    this layer can state the consolidate/decompose boundary without editing
    that cluster. Same name shape, opposite verdict, and only the size tells
    them apart.
    """
    raw = _raw_fixture()["oversize_family"]
    return _page(raw["bare"]), _page(raw["qualified"])


# ---------------------------------------------------------------------------
# Running the shipped decomposition path
# ---------------------------------------------------------------------------


@dataclass
class Outcome:
    """What the librarian did with one case, and which tier decided it."""

    case_id: str
    deciding_tier: str
    decomposed: bool
    escalation: EscalationItem | None
    hub_body: str = ""
    children: dict[str, str] = field(default_factory=dict)  # uid -> body
    child_names: dict[str, str] = field(default_factory=dict)
    edges: list[tuple[str, str, str]] = field(default_factory=list)  # (source, target, role)

    @property
    def conflict_type(self) -> str:
        return self.escalation.conflict_type if self.escalation is not None else ""

    def summary(self) -> str:
        verdict = "decomposed" if self.decomposed else "not decomposed"
        return (
            f"{self.case_id}: {verdict} by {self.deciding_tier} "
            f"(conflict_type={self.conflict_type or 'none'}, "
            f"children={len(self.children)}, split_edges={len(split_edges(self.edges))})"
        )


def _action(case: Case) -> EntityAction:
    return EntityAction(
        kind="update",
        name=case.page.name,
        entity_type=case.page.type,
        tags=list(case.page.tags),
        access=case.page.access,
        existing_uid=case.page.uid,
        observations=case.intake_text,
    )


def materialize(case: Case, root: Path) -> Path:
    """Write the case's hub and any existing sub-pages into a fresh wiki."""
    wiki = root / "wiki"
    wiki.mkdir(parents=True, exist_ok=True)
    assert_disposable(wiki)
    case.page.write(wiki)
    for sub in case.subpages:
        sub.write(wiki)
    return wiki


def _read_edges(wiki: Path) -> list[tuple[str, str, str]]:
    edges: list[tuple[str, str, str]] = []
    for path in sorted(wiki.glob("*.md")):
        meta, _body = parse_frontmatter(path.read_text(encoding="utf-8"))
        source = str(meta.get("uid") or "")
        for row in meta.get("related") or ():
            if isinstance(row, dict):
                edges.append((source, str(row.get("uid", "")), str(row.get("role", ""))))
    return edges


def split_edges(edges: list[tuple[str, str, str]]) -> list[tuple[str, str, str]]:
    """Only the edges the SPLIT wrote -- never athenaeum#1576's term-overlap."""
    return [edge for edge in edges if edge[2] in SPLIT_ROLES]


def observe_default_disposition(case: Case, root: Path) -> Outcome:
    """Run the gate at the SHIPPED DEFAULT and confirm nothing was applied.

    athenaeum#1581 AC4. ``oversize_page_action`` is left unset, so
    :func:`~athenaeum.tiers.resolve_oversize_page_action` resolves ``review``:
    an over-threshold page yields an ``oversize_page`` escalation for the
    pending queue and is left byte-for-byte unmodified, and an under-threshold
    page yields nothing at all. Either way no page is restructured, which is
    the invariant -- the proposal is what the operator sees.
    """
    wiki = materialize(case, root)
    path = wiki / f"{case.page.uid}.md"
    before = path.read_text(encoding="utf-8")
    meta, body = parse_frontmatter(before)

    escalation = check_page_size_gate(
        _action(case),
        body,
        f"eval-decomposition/{case.id}",
        {},
        existing_path=path,
        existing_meta=meta,
        wiki_root=wiki,
    )
    after = path.read_text(encoding="utf-8")
    if after != before:
        raise AssertionError(f"{case.id}: the default disposition modified the page on disk")
    return Outcome(
        case_id=case.id,
        deciding_tier=TIER_SIZE_GATE,
        decomposed=False,
        escalation=escalation,
        hub_body=body,
    )


def run_split_disposition(case: Case, root: Path) -> Outcome:
    """Run the gate with ``oversize_page_action: split`` in a throwaway wiki.

    This is the only shipped code path that expresses a decomposition, so it
    is the only way to grade WHAT one would contain. It restructures pages,
    which is why it runs against a materialized ``tmp_path`` corpus and why
    :func:`assert_disposable` guards the root. What an operator would see for
    these same cases is :func:`observe_default_disposition`'s proposal.
    """
    wiki = materialize(case, root)
    path = wiki / f"{case.page.uid}.md"
    meta, body = parse_frontmatter(path.read_text(encoding="utf-8"))
    before_uids = {p.stem for p in wiki.glob("*.md")}

    escalation = check_page_size_gate(
        _action(case),
        body,
        f"eval-decomposition/{case.id}",
        {"librarian": {"oversize_page_action": "split"}},
        existing_path=path,
        existing_meta=meta,
        wiki_root=wiki,
    )

    hub_meta, hub_body = parse_frontmatter(path.read_text(encoding="utf-8"))
    children: dict[str, str] = {}
    child_names: dict[str, str] = {}
    for child_path in sorted(wiki.glob("*.md")):
        if child_path.stem in before_uids:
            continue
        child_meta, child_body = parse_frontmatter(child_path.read_text(encoding="utf-8"))
        children[str(child_meta.get("uid") or child_path.stem)] = child_body
        child_names[str(child_meta.get("uid") or child_path.stem)] = str(child_meta.get("name", ""))

    return Outcome(
        case_id=case.id,
        deciding_tier=TIER_SIZE_GATE,
        decomposed=bool(children),
        escalation=escalation,
        hub_body=hub_body,
        children=children,
        child_names=child_names,
        edges=_read_edges(wiki),
    )


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FacetScore:
    """How cleanly a decomposition's child pages line up with the facets.

    ``placed`` counts facets whose every ground-truth fact key landed on ONE
    child page. ``scattered`` counts facets whose keys are spread across more
    than one child. ``conflated`` counts child pages carrying keys from more
    than one facet. A split at typographic boundaries that happen not to be
    facet boundaries shows up as both of the latter two, which is exactly the
    failure the issue names ("not arbitrary section boundaries").
    """

    placed: tuple[str, ...]
    scattered: tuple[str, ...]
    conflated: tuple[str, ...]
    missing: tuple[str, ...]

    @property
    def aligned(self) -> bool:
        return not (self.scattered or self.conflated or self.missing)

    def summary(self) -> str:
        return (
            f"placed={list(self.placed)} scattered={list(self.scattered)} "
            f"conflated={list(self.conflated)} missing={list(self.missing)}"
        )


def facet_alignment(outcome: Outcome, facets: dict[str, tuple[str, ...]]) -> FacetScore:
    """Score *outcome*'s child pages against the case's facet ground truth.

    Fact keys are matched as SUBSTRINGS of a child body, never as exact
    prose -- the same "shape, not wording" contract every other layer scores
    under. A facet whose keys appear on no child at all is ``missing``, which
    is what a page that did not decompose scores for every one of its facets.
    """
    placed: list[str] = []
    scattered: list[str] = []
    missing: list[str] = []
    facets_per_child: dict[str, set[str]] = {uid: set() for uid in outcome.children}

    for facet, keys in sorted(facets.items()):
        holders: set[str] = set()
        for uid, body in outcome.children.items():
            if any(contains_fact(body, key) for key in keys):
                holders.add(uid)
                facets_per_child[uid].add(facet)
        if not holders:
            missing.append(facet)
        elif len(holders) == 1:
            placed.append(facet)
        else:
            scattered.append(facet)

    conflated = sorted(uid for uid, held in facets_per_child.items() if len(held) > 1)
    return FacetScore(
        placed=tuple(placed),
        scattered=tuple(scattered),
        conflated=tuple(conflated),
        missing=tuple(missing),
    )


def score_case(case: Case, outcome: Outcome) -> tuple[bool, str]:
    """Did the librarian reach this case's ground-truth verdict?

    A ``no_decompose`` case passes on the verdict alone -- there is nothing
    to align when nothing was split. A ``decompose`` case must ALSO place
    every facet on its own child page; "it split, somewhere" is the result
    this measure exists to reject.

    Only cases the SIZE GATE decides are scored here. A case whose
    ``deciding_tier`` is :data:`TIER_CLASSIFY` (Case D: an already-decomposed
    hub, where what is in question is where new intake LANDS, not whether a
    page is cut up) has no size-gate verdict to score, and silently reading
    its "the gate did nothing" as a pass would be exactly the vacuous result
    this layer exists to catch. It is scored in
    ``tests/evals/test_decomposition_eval.py`` against a real classify call.
    """
    if case.deciding_tier != TIER_SIZE_GATE:
        raise ValueError(
            f"{case.id}: decided by {case.deciding_tier!r}, not the size gate -- "
            "score it against the tier that actually decides it"
        )
    if not case.expects_decompose:
        if outcome.decomposed:
            return False, f"{case.id}: decomposed a page that should not be ({outcome.summary()})"
        return True, f"{case.id}: correctly left intact ({outcome.summary()})"

    if not outcome.decomposed:
        return False, f"{case.id}: no decomposition proposed ({outcome.summary()})"

    score = facet_alignment(outcome, case.facets)
    if not score.aligned:
        return False, f"{case.id}: decomposed but misaligned -- {score.summary()}"

    hub_uid = case.page.uid
    child_backlinks = {
        source
        for source, target, role in outcome.edges
        if role == ROLE_SPLIT_FROM and target == hub_uid
    }
    if child_backlinks != set(outcome.children):
        return False, f"{case.id}: not every child carries a {ROLE_SPLIT_FROM!r} edge to the hub"
    hub_links = {
        target
        for source, target, role in outcome.edges
        if role == ROLE_SPLIT_INTO and source == hub_uid
    }
    if hub_links != set(outcome.children):
        return False, f"{case.id}: the hub does not carry a {ROLE_SPLIT_INTO!r} edge to every child"

    return True, f"{case.id}: decomposed along facets -- {score.summary()}"


# ---------------------------------------------------------------------------
# The consolidate / decompose boundary
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BoundaryVerdict:
    """One ``name`` / ``name (qualifier)`` family, read at the boundary."""

    family: str
    merged_chars: int
    threshold: int
    verdict: str  # "consolidate" | "decompose"

    def summary(self) -> str:
        return (
            f"{self.family}: merged body would be {self.merged_chars} chars "
            f"against a {self.threshold}-char threshold -> {self.verdict}"
        )


def boundary_verdict(
    family: str, bodies: list[str], *, config: dict[str, Any] | None = None
) -> BoundaryVerdict:
    """Apply :func:`athenaeum.name_structure.merged_body_within_page_size_threshold`.

    Reached through the production predicate rather than re-implemented here,
    for the reason ``tests/evals/relatedness_writer.py`` states about the
    relatedness writer: a measure that reimplements the rule it grades
    verifies a copy, and leaves production free to diverge from it.
    """
    within = merged_body_within_page_size_threshold(bodies, config=config)
    return BoundaryVerdict(
        family=family,
        merged_chars=sum(len(b) for b in bodies),
        threshold=resolve_page_size_threshold_chars(config),
        verdict="consolidate" if within else "decompose",
    )
