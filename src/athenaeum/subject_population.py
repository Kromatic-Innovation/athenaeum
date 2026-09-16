# SPDX-License-Identifier: Apache-2.0
"""Meaning-based ``subject`` population, dry-run by default (athenaeum#1714).

Step 1 of the operator-approved plan for athenaeum#1244: give Gate 1 a real
``subject`` to separate on. athenaeum#1656 deleted the old ``subject := uid``
derivation (``subject_backfill.py`` / ``_cmd_subject.py``) outright, leaving
NO ``subject``-population code path on ``develop`` — this module is that
build, not a replacement of live code (see ``tests/test_subject_backfill.py``,
which still pins the old CLI command gone; this module never resurrects it,
see "CLI surface" below).

**A page's ``subject`` is a MINTED id per real-world thing** (operator
decision 1, recorded 2026-09-16 on this issue) — never a page's own ``uid``,
never an exact-name string. :class:`SubjectRegistry` is the "small subject
registry" the issue asks for: a monotonic id counter plus, per minted id,
the member uids that currently carry it.

**Algorithm** (issue Plan steps 1-8), per entity type (matching
:func:`athenaeum.entity_resolution.resolve_same_subject`'s own type-scoping
convention, via :meth:`athenaeum.models.EntityIndex.pages_of_type`):

1. Seed a "resolved pool" from comparator-eligible pages of this type that
   ALREADY carry a real (non-``undeterminable``) ``subject:`` — an
   idempotent re-run compares new pages against these too, matching the
   issue's own "new/unresolved page against ... existing resolved pages"
   phrasing.
2. For every remaining (subject-less) eligible page of this type, in
   deterministic (filename-sorted) order, call
   :func:`~athenaeum.entity_resolution.resolve_same_subject` against the
   resolved pool so far.
3. ``Match`` -> write the SAME subject id the matched pool member carries.
   The matched page joins the pool under that id.
4. ``Ambiguous`` -> ``subject: undeterminable`` + (on ``--apply``) a pending
   question via :func:`athenaeum.answers.raise_pending_question`. Not added
   to the pool — there is no single confident id to compare future pages
   against.
5. Genuine ``NoMatch`` (a real "nothing plausible" decision, INCLUDING the
   legitimate first-page-of-its-kind case where the pool is empty) -> mint a
   new subject id. The page joins the pool under that new id.
6. A DEGRADED run (embedder unavailable, no confirmer wired, confirmer
   errored, or confirmer named a uid outside its own candidate set) also
   surfaces as ``NoMatch`` from ``resolve_same_subject`` — its return type
   alone cannot tell that apart from step 5's genuine no-match (see its own
   docstring on ``NoMatch``). :func:`_resolve_with_degradation_tracking`
   is the call-site fix: it wraps the *embedder*/*confirm* callables this
   module passes in so THIS caller observes which branch fired, without
   touching ``resolve_same_subject`` itself (out of scope here — this issue
   is a caller, not a resolver change). A degraded outcome always records
   ``subject: undeterminable``, never a mint, never a guess.
7. Ratification (operator decision 2): a subject counts as ratified the
   moment the tier-2 confirmer returns a Match/mint decision — no
   additional human-confirmation gate. :func:`build_tier2_confirm` wires
   :func:`athenaeum.tiers._tier2_confirm_same_subject` in exactly the way
   :func:`athenaeum.tiers.validate_create_name` already wires it into
   ``resolve_same_subject`` for the create path.
8. Scope (operator decision 3): only pages
   :func:`athenaeum.wiki_dedupe.discover_wiki_dedupe_candidates` returns are
   processed — see "Comparator-eligible" below.

**Dry run by default.** :func:`run_subject_population` never writes to
*wiki_root* unless ``apply=True`` is passed explicitly — matching this
issue's own "Dry run by default" section (applying to the live corpus is
athenaeum#1244's own separately-gated step).

**Comparator-eligible, and which predicate this reuses.** ``cluster_comparator
.py``'s own module docstring says :func:`athenaeum.comparator.compare_pages`
(the PAIRWISE wiki-page comparator this issue's "comparator-eligible" refers
to) "is called only from ``wiki_dedupe.py``, ``comparator.py`` ... and
``recompare.py``". :func:`athenaeum.wiki_dedupe.discover_wiki_dedupe_candidates`
is that domain's own eligibility predicate (type in
:data:`athenaeum.wiki_dedupe.DEDUPE_CANDIDATE_TYPES`, storage-adapter
``merge_eligible``, not archived/superseded/pointer-stub/pii-flagged, and an
optional min-body-chars floor) — reused here verbatim via a direct call
rather than re-implemented, so this pass can never drift from the actual
wiki-page comparator's own candidate pool. ``cluster_comparator.py`` itself
has no eligibility predicate over wiki pages at all — its domain is
auto-memory clusters, a different input entirely.

**CLI surface: deliberately none.** ``tests/test_subject_backfill.py``
forbids exactly two things: a top-level ``subject`` CLI subcommand, and any
``--apply`` invocation through it succeeding. This module adds neither — it
is library-only, with no ``_cmd_*`` module and no ``cli.py`` wiring, so
there is no CLI surface for that regression test's assertions to even
reach. A future issue that wants an operator-facing entry point should pick
a name that is not ``subject`` (the regression test only pins that literal
string absent from the top-level subcommand choices), but this issue does
not need one: its own deliverable is the dry-run report, exercised directly
by :func:`run_subject_population` from tests.

Layering: L4 (domain/pipeline). Imports :mod:`athenaeum.answers`,
:mod:`athenaeum.entity_resolution`, :mod:`athenaeum.wiki_dedupe` (all L4),
:mod:`athenaeum.models` (L1), :mod:`athenaeum.atomic_io` (L0), and
:mod:`athenaeum.search` (L3, for the default embedder) — all at or below
this module's own layer.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Sequence

import yaml

from athenaeum.answers import raise_pending_question
from athenaeum.atomic_io import atomic_write_text
from athenaeum.entity_resolution import (
    DEFAULT_TOP_K,
    Ambiguous,
    ConfirmFn,
    EmbedFn,
    Match,
    NoMatch,
    ResolutionResult,
    SubjectPage,
    resolve_same_subject,
)
from athenaeum.models import EntityIndex, parse_frontmatter
from athenaeum.search import embed_texts
from athenaeum.wiki_dedupe import DEDUPE_CANDIDATE_TYPES, discover_wiki_dedupe_candidates

if TYPE_CHECKING:
    from athenaeum.tiers import LLMBackend, TokenUsage

log = logging.getLogger(__name__)

#: The literal recorded for an Ambiguous result or a degraded resolver run
#: (issue Plan steps 4/6). Never a guessed subject.
UNDETERMINABLE = "undeterminable"

#: Registry sidecar filename, one per wiki root — "a small subject
#: registry" (operator decision 1). JSON, not YAML/frontmatter: it is not
#: itself a wiki page.
SUBJECT_REGISTRY_FILENAME = "_subject_registry.json"

#: Same shape as ``memory_class_backfill._FRONTMATTER_RE`` /
#: ``page_description._FRONTMATTER_RE`` — re-declared per that established
#: per-module convention (each ``insert_<field>`` sidecar owns its own
#: regex) rather than a new shared import.
_FRONTMATTER_RE = re.compile(r"^---\s*\r?\n(.*?)\r?\n---\s*\r?\n", re.DOTALL)


@dataclass
class SubjectRegistry:
    """The small store mapping a minted ``subject`` id to its member uids.

    ``subjects`` maps a minted id (``"subject-000001"``, ...) to the list of
    uids currently carrying it. Deliberately tiny — no schema beyond the
    counter and that one mapping (issue: "keep it small and obvious").
    """

    subjects: dict[str, list[str]] = field(default_factory=dict)
    next_id: int = 1

    def mint(self, uid: str) -> str:
        """Allocate a brand-new subject id and register *uid* as its first member."""
        subject_id = f"subject-{self.next_id:06d}"
        self.next_id += 1
        self.subjects[subject_id] = [uid]
        return subject_id

    def record_match(self, subject_id: str, uid: str) -> None:
        """Add *uid* as an additional member of an already-minted *subject_id*."""
        members = self.subjects.setdefault(subject_id, [])
        if uid not in members:
            members.append(uid)

    def to_dict(self) -> dict[str, Any]:
        return {"next_id": self.next_id, "subjects": self.subjects}

    @classmethod
    def load(cls, path: Path) -> "SubjectRegistry":
        """Read an existing registry, or start fresh if absent/unreadable.

        Read-only — never writes. Safe to call from a dry run: it only
        changes what the IN-MEMORY report previews, never *path* itself.
        """
        if not path.exists():
            return cls()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        subjects_raw = raw.get("subjects") if isinstance(raw, dict) else None
        subjects = {
            str(k): [str(v) for v in vs]
            for k, vs in subjects_raw.items()
            if isinstance(vs, list)
        } if isinstance(subjects_raw, dict) else {}
        try:
            next_id = int(raw.get("next_id", 1)) if isinstance(raw, dict) else 1
        except (TypeError, ValueError):
            next_id = 1
        return cls(subjects=subjects, next_id=max(next_id, 1))

    def save(self, path: Path) -> None:
        atomic_write_text(path, json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n")


@dataclass(frozen=True)
class PageDecision:
    """What this pass decided for one comparator-eligible page, and why.

    ``subject`` is the value that WOULD be (or, on ``apply``, was) written —
    either a real minted/matched subject id, or the literal
    :data:`UNDETERMINABLE`. ``reason`` is a closed vocabulary: ``"matched"``,
    ``"minted"``, ``"undeterminable-ambiguous"``, ``"undeterminable-degraded"``.
    """

    uid: str
    name: str
    type: str
    path: Path
    subject: str
    reason: str
    matched_uid: str | None = None


@dataclass
class SubjectPopulationReport:
    """Counts + per-page decisions for one dry-run or apply pass."""

    scanned: int = 0
    decisions: list[PageDecision] = field(default_factory=list)

    @property
    def matched(self) -> list[PageDecision]:
        return [d for d in self.decisions if d.reason == "matched"]

    @property
    def minted(self) -> list[PageDecision]:
        return [d for d in self.decisions if d.reason == "minted"]

    @property
    def undeterminable(self) -> list[PageDecision]:
        return [d for d in self.decisions if d.reason.startswith("undeterminable")]

    def counts(self) -> dict[str, int]:
        return {
            "scanned": self.scanned,
            "matched_existing_subject": len(self.matched),
            "minted_new_subject": len(self.minted),
            "undeterminable": len(self.undeterminable),
        }


def build_tier2_confirm(
    client: "LLMBackend",
    *,
    config: dict[str, Any] | None = None,
    usage: "TokenUsage | None" = None,
) -> ConfirmFn:
    """Wire :func:`athenaeum.tiers._tier2_confirm_same_subject` as a confirmer.

    Issue Plan step 7 / operator decision 2: ratification happens the moment
    this confirmer (not an extra human-confirmation step) returns a
    Match/mint decision. Wired the same way
    :func:`athenaeum.tiers.validate_create_name` already wires it into
    ``resolve_same_subject`` for the create path. Function-local import —
    matches this module's own L4-peer-import discipline and
    :mod:`athenaeum.tiers`'s own deferred-SDK-import convention.
    """
    from athenaeum.tiers import _tier2_confirm_same_subject

    def confirm(
        candidate: SubjectPage, top: Sequence[tuple[SubjectPage, float]]
    ) -> ResolutionResult:
        # _tier2_confirm_same_subject's own annotation is the looser
        # `Ambiguous | Match | object` (see its module); every actual
        # return statement in its body constructs Match/Ambiguous/NoMatch,
        # so this narrows back to this module's ResolutionResult contract.
        result = _tier2_confirm_same_subject(
            candidate, top, client=client, config=config, usage=usage
        )
        assert isinstance(result, (Match, Ambiguous, NoMatch))
        return result

    return confirm


def _resolve_with_degradation_tracking(
    candidate: SubjectPage,
    existing_pages: Sequence[SubjectPage],
    *,
    embedder: EmbedFn | None,
    confirm: ConfirmFn | None,
    config: dict[str, Any] | None,
    top_k: int,
) -> tuple[ResolutionResult, bool]:
    """Call :func:`resolve_same_subject`, additionally reporting degradation.

    ``resolve_same_subject``'s return type alone cannot distinguish a
    genuine ``NoMatch`` from a DEGRADED one (embedder unavailable, no
    confirmer, confirmer error, or confirmer naming a uid outside its own
    candidate set — see its docstring). This wraps *embedder*/*confirm* so
    THIS call site observes which branch actually fired, without changing
    ``resolve_same_subject`` itself.

    Returns ``(result, False)`` untouched when *existing_pages* is empty:
    ``resolve_same_subject``'s own empty-pool short-circuit never calls
    either callable, so there is nothing to degrade — a genuinely first
    page of its kind, not a degraded run.
    """
    if not existing_pages:
        result = resolve_same_subject(
            candidate,
            existing_pages,
            embedder=embedder,
            confirm=confirm,
            config=config,
            top_k=top_k,
        )
        return result, False

    degraded = False
    real_embed = embedder if embedder is not None else embed_texts

    def tracking_embed(texts: list[str]) -> "list[list[float]] | None":
        nonlocal degraded
        vectors = real_embed(texts)
        if vectors is None:
            degraded = True
        return vectors

    real_confirm = confirm

    def tracking_confirm(
        cand: SubjectPage, top: Sequence[tuple[SubjectPage, float]]
    ) -> ResolutionResult:
        nonlocal degraded
        if real_confirm is None:
            # No confirmer wired at all. resolve_same_subject only reaches
            # this call when embedding similarity already surfaced at
            # least one candidate above threshold -- a real signal, just
            # unconfirmed. Mirrors resolve_same_subject's own
            # `degraded=no-confirmer` branch, from a call site that can
            # act on it rather than only log it.
            degraded = True
            return NoMatch()
        try:
            result = real_confirm(cand, top)
        except Exception:
            degraded = True
            raise
        if isinstance(result, Match):
            top_uids = {p.uid for p, _ in top if p.uid is not None}
            if result.uid not in top_uids:
                # Mirrors resolve_same_subject's own
                # `degraded=confirm-uid-outside-candidates` downgrade to
                # NoMatch -- computed independently here because that
                # downgrade happens AFTER confirm returns, inside
                # resolve_same_subject, where this caller cannot observe it.
                degraded = True
        return result

    result = resolve_same_subject(
        candidate,
        existing_pages,
        embedder=tracking_embed,
        confirm=tracking_confirm,
        config=config,
        top_k=top_k,
    )
    return result, degraded


def _read_existing_subject(path: Path) -> str | None:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    meta, _body = parse_frontmatter(text)
    if not meta:
        return None
    value = meta.get("subject")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def build_subject_population_report(
    wiki_root: Path,
    *,
    embedder: EmbedFn | None = None,
    confirm: ConfirmFn | None = None,
    config: dict[str, Any] | None = None,
    top_k: int = DEFAULT_TOP_K,
    registry: SubjectRegistry | None = None,
) -> SubjectPopulationReport:
    """Pure dry-run pass: decide every comparator-eligible page's subject.

    Reads *wiki_root* and *registry* (if supplied) but writes nothing —
    this is the whole of the default (``apply=False``) behaviour. *registry*
    is mutated in memory (mint counters advance, matches are recorded) so a
    caller that goes on to apply the report can persist the same ids this
    report already decided; pass a fresh :class:`SubjectRegistry` (the
    default) for a pure preview that never needs to match a prior run.
    """
    if registry is None:
        registry = SubjectRegistry()

    index = EntityIndex(wiki_root)
    eligible_paths = {c.path for c in discover_wiki_dedupe_candidates(wiki_root, config=config)}

    report = SubjectPopulationReport()

    for entity_type in sorted(DEDUPE_CANDIDATE_TYPES):
        type_pages = [
            (uid, name, path)
            for uid, name, path in index.pages_of_type(entity_type)
            if path in eligible_paths
        ]

        resolved_pool: list[SubjectPage] = []
        pool_subjects: dict[str, str] = {}
        unresolved: list[tuple[str, str, Path]] = []

        for uid, name, path in type_pages:
            existing_subject = _read_existing_subject(path)
            if existing_subject and existing_subject != UNDETERMINABLE:
                resolved_pool.append(SubjectPage(uid=uid, name=name, type=entity_type, path=path))
                pool_subjects[uid] = existing_subject
                registry.record_match(existing_subject, uid)
            else:
                unresolved.append((uid, name, path))

        for uid, name, path in unresolved:
            report.scanned += 1
            candidate = SubjectPage(uid=uid, name=name, type=entity_type, path=path)
            result, degraded = _resolve_with_degradation_tracking(
                candidate,
                resolved_pool,
                embedder=embedder,
                confirm=confirm,
                config=config,
                top_k=top_k,
            )

            if degraded:
                report.decisions.append(
                    PageDecision(
                        uid, name, entity_type, path, UNDETERMINABLE, "undeterminable-degraded"
                    )
                )
                continue

            if isinstance(result, Ambiguous):
                report.decisions.append(
                    PageDecision(
                        uid, name, entity_type, path, UNDETERMINABLE, "undeterminable-ambiguous"
                    )
                )
                continue

            if isinstance(result, Match):
                subject_id = pool_subjects.get(result.uid)
                if subject_id is None:
                    # Defensive: resolve_same_subject only ever returns a
                    # Match whose uid is one of the pool it was given (see
                    # its own docstring); every pool member this loop adds
                    # is keyed in pool_subjects at the same time. Treat an
                    # inconsistency as undeterminable rather than guess.
                    report.decisions.append(
                        PageDecision(
                            uid, name, entity_type, path, UNDETERMINABLE, "undeterminable-degraded"
                        )
                    )
                    continue
                report.decisions.append(
                    PageDecision(
                        uid, name, entity_type, path, subject_id, "matched", matched_uid=result.uid
                    )
                )
                resolved_pool.append(candidate)
                pool_subjects[uid] = subject_id
                registry.record_match(subject_id, uid)
                continue

            # Genuine NoMatch (not degraded, including the legitimate
            # empty-pool first-of-its-kind case): mint.
            subject_id = registry.mint(uid)
            report.decisions.append(
                PageDecision(uid, name, entity_type, path, subject_id, "minted")
            )
            resolved_pool.append(candidate)
            pool_subjects[uid] = subject_id

    return report


def insert_subject(text: str, subject: str) -> str | None:
    """Return *text* with a ``subject:`` line appended to its frontmatter.

    Returns ``None`` when *text* has no frontmatter block. Textual
    insertion only — touches no other byte of the file, matching
    ``memory_class_backfill.insert_memory_class`` / ``page_description.
    insert_description``'s byte-level-idempotence convention. Rendered
    through ``yaml.dump`` (not an f-string) so a subject id that happens to
    look numeric is still quoted correctly.
    """
    match = _FRONTMATTER_RE.match(text)
    if match is None:
        return None
    end = match.end(1)
    newline = "\r\n" if "\r\n" in text[: match.end()] else "\n"
    line = yaml.dump(
        {"subject": subject}, default_flow_style=False, allow_unicode=True
    ).rstrip("\n")
    return f"{text[:end]}{newline}{line}{text[end:]}"


def apply_subject_population(
    report: SubjectPopulationReport,
    registry: SubjectRegistry,
    *,
    wiki_root: Path,
    pending_path: Path | None = None,
) -> int:
    """Write every decision in *report*: page frontmatter + the registry.

    Re-checks each file's ``subject:`` at write time rather than trusting
    the report (never overwrites an already-set value — idempotent).
    ``undeterminable-ambiguous`` decisions additionally raise a pending
    question via :func:`athenaeum.answers.raise_pending_question` (issue
    Plan step 4) before the page write. Returns the number of page files
    changed. This is the ONLY function in this module that writes to
    *wiki_root* — never called by :func:`run_subject_population` unless
    ``apply=True``.
    """
    if pending_path is None:
        pending_path = wiki_root / "_pending_questions.md"

    changed = 0
    for decision in report.decisions:
        if decision.reason == "undeterminable-ambiguous":
            raise_pending_question(
                pending_path,
                question=(
                    f"Which existing subject, if any, is {decision.name!r} "
                    f"({decision.uid}) the same real-world thing as?"
                ),
                context=(
                    "athenaeum#1714 meaning-based subject population found "
                    f"{decision.name!r} ({decision.uid}, type={decision.type}) "
                    "plausibly matches more than one existing subject and "
                    "could not confirm a single one automatically."
                ),
                entity=decision.name,
                source=str(decision.path),
            )

        try:
            text = decision.path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        meta, _body = parse_frontmatter(text)
        existing = meta.get("subject") if meta else None
        if isinstance(existing, str) and existing.strip():
            continue
        updated = insert_subject(text, decision.subject)
        if updated is None or updated == text:
            continue
        atomic_write_text(decision.path, updated)
        changed += 1

    registry.save(wiki_root / SUBJECT_REGISTRY_FILENAME)
    return changed


def run_subject_population(
    wiki_root: Path,
    *,
    apply: bool = False,
    embedder: EmbedFn | None = None,
    confirm: ConfirmFn | None = None,
    config: dict[str, Any] | None = None,
    top_k: int = DEFAULT_TOP_K,
    pending_path: Path | None = None,
) -> SubjectPopulationReport:
    """The one entry point: dry-run by default, writes only when ``apply=True``.

    Always builds the SubjectRegistry from any existing
    ``_subject_registry.json`` under *wiki_root* first (a read — safe in a
    dry run) so a repeated dry-run preview and a real ``apply`` run agree
    on which ids already exist. Only :func:`apply_subject_population`
    (called here only when *apply* is ``True``) writes anything.
    """
    registry = SubjectRegistry.load(wiki_root / SUBJECT_REGISTRY_FILENAME)
    report = build_subject_population_report(
        wiki_root,
        embedder=embedder,
        confirm=confirm,
        config=config,
        top_k=top_k,
        registry=registry,
    )
    if apply:
        apply_subject_population(report, registry, wiki_root=wiki_root, pending_path=pending_path)
    return report


__all__ = [
    "PageDecision",
    "SubjectPopulationReport",
    "SubjectRegistry",
    "UNDETERMINABLE",
    "apply_subject_population",
    "build_subject_population_report",
    "build_tier2_confirm",
    "insert_subject",
    "run_subject_population",
]
