# SPDX-License-Identifier: Apache-2.0
"""Decompose an aggregate page into per-entity facts (issue athenaeum#1914).

One page in the live corpus is a `tool` page in name only: its body is a flat
list of relationship facts about individual companies and people, each
footnoted to a raw source. The facts belong on the pages they concern; the
tool page should describe the tool. Deleting the page would lose the facts, so
it is held out of the retire sweep until they are redistributed.

**Why a new pass rather than an existing write path.** The specify pass on
athenaeum#1914 established that neither sanctioned ingress can carry a
*body-level* fact from one page to another:

* Field corrections are frontmatter-only by contract
  (``docs/design/field-corrections.md`` §14). Their one body-writing branch
  (:func:`athenaeum.corrections._record_as_prose`) fires only for an
  allowlisted attribute with a ``schema_slots: {prose: true}`` entry, renders
  ``- <today>: `field` = '<repr>'``, and flattens the footnote to one inline
  source string — so it would need a permanent host-config change AND would
  still lose the footnote.
* Raw intake would hand each fact to the LLM tiers, whose programmatic
  mention-match would fan every note back into the very aggregate pages this
  pass exists to undo, at metered cost, paraphrasing the fact and replacing
  its ``drive/`` footnote with the note's own.

So this module is a deterministic, dry-run-default, single-page maintenance
pass in the shape the librarian already uses for
:mod:`athenaeum.paste_cleanup` and :mod:`athenaeum.retire_pages`: read, build
a report, and only under ``--apply`` write through
:func:`athenaeum.atomic_io.atomic_write_text`. It is athenaeum code acting AS
the librarian under the run lock, not a source writing the store, so the
one-way-in ingress invariant holds. **It makes no LLM call**: by default every
bullet is already one fact per line with the subject as its leading span, so
there is nothing to split and nothing to classify beyond the whole item.

**Clause mode (``split_clauses``, issue athenaeum#1947).** Some aggregate
pages do not hold that invariant: one top-level list item can carry many
footnote-cited facts run together with no line break between them (the
athenaeum#1942 merge-writer interleave is one way this happens). Passing
``split_clauses=True`` to :func:`build_report` (CLI: ``--split-clauses``)
turns such a run-on item into one candidate per cited clause before
classification, deterministically: a clause ends at a run of inline footnote
markers, optionally followed by one of ``.``/``;``/``,`` and then whitespace
or the end of the item (see :func:`split_into_clauses`). An item is only
split when that rule yields 2+ clauses AND ``--subject-until`` matches it 2+
times; otherwise it is kept whole, exactly as today. A clause where
``--subject-until`` matches more than once, or not at all, is ``malformed``
— reported ``unresolved`` with an empty subject rather than re-cut by a
second heuristic. This still makes no LLM call: the clause boundary is pure
text arithmetic over the same two regexes the rest of this module already
uses.

**What it refuses to guess.** Three separate failure classes each get their
own disposition and each needs an explicit operator ruling before ``--apply``
will touch anything:

* ``unresolved`` — the subject does not resolve to a ``company``/``person``
  page. The subject is the bullet's leading span up to the first match of a
  caller-supplied ``subject_until`` regex, and nothing else: a
  longest-index-key-prefix rule was considered and rejected because it
  silently attaches "X Labs" to a shorter existing entity "X".
* ``no-source`` — the bullet carries no footnote reference, so attaching it
  would move a fact without its provenance.
* ``ambiguous-source`` — the bullet's label has two or more conflicting
  definitions and the ``drive/<id>-<slug>.md`` slug rule picks none of them.
  Attaching a fact under another company's source is worse than not
  attaching it.

Layering: L4 domain/pipeline — same layer as
:mod:`athenaeum.entity_resolution`, which it imports for
:func:`~athenaeum.entity_resolution.normalize_name` (same-layer imports are
allowed). Everything else it reads is below it:
:mod:`athenaeum.footnote_markers` (L1) for the two regexes that tell an
inline marker from a definition, :mod:`athenaeum.models` (L1),
:mod:`athenaeum.schemas` (L1) and :mod:`athenaeum.atomic_io` (L0).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path

from pydantic import ValidationError

from athenaeum.atomic_io import atomic_write_text
from athenaeum.entity_resolution import normalize_name
from athenaeum.footnote_markers import FOOTNOTE_DEF_RE, INLINE_MARKER_RE
from athenaeum.models import EntityIndex, parse_frontmatter, render_frontmatter, slugify
from athenaeum.schemas import validate_wiki_meta

log = logging.getLogger(__name__)

#: Report schema version. Bump when a consumer-visible field changes shape.
DECOMPOSE_REPORT_VERSION = 1

#: The ``--split-clauses`` report shape (issue athenaeum#1947): a strict
#: superset of version 1 (see :class:`DecomposeReport`). ``build_report``
#: without ``split_clauses=True`` still always produces
#: :data:`DECOMPOSE_REPORT_VERSION`.
DECOMPOSE_REPORT_VERSION_2 = 2

#: The only page types a bullet subject may resolve to. A bullet is about a
#: company or a person; resolving one to a `tool` or `concept` page would be
#: the aggregate-page mistake this pass exists to undo.
RESOLVABLE_TYPES: frozenset[str] = frozenset({"company", "person"})

#: Dispositions, in the order :func:`classify` decides them. Exactly one is
#: assigned to every bullet.
DISPOSITIONS: tuple[str, ...] = (
    "attached",
    "already-present",
    "unresolved",
    "no-source",
    "ambiguous-source",
)

#: Dispositions that block ``--apply`` until the operator rules on them.
BLOCKING_DISPOSITIONS: frozenset[str] = frozenset(
    {"unresolved", "no-source", "ambiguous-source"}
)

#: A body bullet: ``- <subject> <fact>[^N]``. Footnote DEFINITION lines start
#: with ``[^``, never ``-``, so they can never match this.
BULLET_RE = re.compile(r"^-[ \t]+(\S.*)$")

#: A ``drive/<id>-<slug>.md`` reference inside a footnote definition. The slug
#: is everything after the FIRST hyphen of the basename, which is the shape
#: the store's raw-file names take.
DRIVE_PATH_RE = re.compile(r"drive/([^/`\s\"]+?)\.md")

#: A double-quoted span inside a bullet — the "quoted engagement string" half
#: of the already-present conjunction.
QUOTED_RE = re.compile(r'"([^"\n]+)"')

#: Markdown wikilink in a rewrite body, e.g. ``[[Pipeline Directory]]``.
WIKILINK_RE = re.compile(r"\[\[([^\]|]+?)(?:\|[^\]]*)?\]\]")

#: Inline markdown link to a sibling page, e.g. ``[text](some-page.md)``.
MD_LINK_RE = re.compile(r"\]\(([^)\s]+\.md)\)")

#: Hard ceiling on the rewritten source body (issue athenaeum#1914 AC): the
#: point of the rewrite is that the page stops being an aggregate.
MAX_REWRITE_BODY_BYTES = 2048

#: A run of one or more inline footnote markers with nothing between them --
#: ``[^a][^b]``, never ``[^a] [^b]``. The clause terminator (issue
#: athenaeum#1947, see :func:`split_into_clauses`) is built on this: a marker
#: run that is NOT followed by whitespace/end-of-item (optionally through one
#: of ``.``/``;``/``,``) is not a terminator at all, which is exactly how an
#: athenaeum#1942 interleave (a marker glued directly to the next clause's
#: text) produces one malformed clause instead of a silent mis-split.
CLAUSE_MARKER_RUN_RE = re.compile(r"(?:\[\^[^\]\s]+\](?!:))+")

#: The three punctuation characters a clause terminator may optionally
#: consume right after its marker run, before the whitespace/end-of-item that
#: the terminator itself requires.
_CLAUSE_TERMINATOR_PUNCT = ".;,"

#: Clause shapes a bullet (whole item or split clause) can carry under
#: ``split_clauses`` (issue athenaeum#1947). ``"item"`` — the version 1 shape,
#: used both when ``split_clauses`` is off and when an eligible item was not
#: split. ``"clause"`` — a well-formed split clause (``--subject-until``
#: matched exactly once). ``"malformed"`` — a split clause the terminator
#: rule produced but the template did not match exactly once; always
#: ``unresolved`` with an empty subject.
CLAUSE_SHAPES: tuple[str, ...] = ("item", "clause", "malformed")


class DecomposeError(Exception):
    """A refusal that must abort ``--apply`` before anything is written."""


@dataclass(frozen=True)
class Definition:
    """One footnote-definition line on the source page."""

    label: str
    text: str
    #: The ``drive/<id>-<slug>.md`` path this definition cites, or ``""``.
    drive_path: str

    @property
    def slug(self) -> str:
        """The ``<slug>`` half of ``drive/<id>-<slug>.md``, or ``""``.

        The id is everything up to the FIRST hyphen; the slug is the rest.
        A path with no hyphen has no slug and can never win the slug rule.
        """
        if not self.drive_path:
            return ""
        stem = self.drive_path.rsplit("/", 1)[-1]
        if stem.endswith(".md"):
            stem = stem[: -len(".md")]
        _, sep, rest = stem.partition("-")
        return rest if sep else ""


#: Field names a version 1 bullet dict carries, in order. Fixed, not derived
#: from ``BulletPlan``'s dataclass fields, so a FUTURE field added to
#: ``BulletPlan`` defaults to invisible on a version 1 report instead of
#: silently widening it -- see ``DecomposeReport._bullet_dict``.
_V1_BULLET_FIELDS: tuple[str, ...] = (
    "ordinal",
    "id",
    "raw",
    "subject",
    "refs",
    "disposition",
    "uid",
    "target",
    "source",
    "source_drive_path",
    "note",
)


@dataclass
class BulletPlan:
    """One source bullet and everything decided about it."""

    #: 1-based position in the source body.
    ordinal: int
    #: Stable id: ``<ordinal>-<first 12 hex of sha256(raw)>``.
    id: str
    #: The bullet line verbatim, including its leading ``- ``.
    raw: str
    #: The leading span up to ``subject_until``; ``""`` when it did not match.
    subject: str
    #: Inline footnote labels the bullet references, in order.
    refs: list[str]
    disposition: str
    #: Resolved target uid, or ``""``.
    uid: str = ""
    #: Resolved target page filename, or ``""``.
    target: str = ""
    #: The slug-selected definition's text, or ``""``.
    source: str = ""
    #: The slug-selected definition's ``drive/`` path, or ``""``.
    source_drive_path: str = ""
    #: Why a bullet landed on a non-``attached`` disposition, for the operator.
    note: str = ""
    #: ``split_clauses`` fields (issue athenaeum#1947). Appended last, and
    #: never serialized for a version 1 report (see
    #: ``DecomposeReport._bullet_dict``), so they cannot perturb the
    #: backwards-compatible default-path byte shape.
    #: 1-based position of the ENCLOSING list item -- equals ``ordinal`` for
    #: every bullet, whole item or clause alike, which is what lets a clause
    #: and its siblings be grouped back under one item.
    item_ordinal: int = 0
    #: 1-based position of this clause within its item; ``0`` for a whole
    #: item (``clause_shape == "item"``).
    clause_index: int = 0
    #: One of :data:`CLAUSE_SHAPES`.
    clause_shape: str = "item"


@dataclass
class DecomposeReport:
    """The dry-run report. Written to a host path, never to stdout."""

    source_uid: str
    source_name: str
    source_path: str
    subject_until: str
    bullets: list[BulletPlan] = field(default_factory=list)
    #: Distinct labels defined on the page that NO bullet references.
    orphan_definitions: int = 0
    #: Distinct labels carrying two or more conflicting definitions.
    conflicting_labels: int = 0
    version: int = DECOMPOSE_REPORT_VERSION
    #: ``split_clauses`` top-level fields (issue athenaeum#1947) -- a version
    #: 1 report leaves these at their defaults and never serializes them
    #: (see ``to_dict``). ``items`` is the top-level list-item count (every
    #: version 1 ``bullet_count`` was this, back when an item WAS a bullet).
    items: int = 0
    #: Items the terminator + ``--subject-until`` eligibility rule actually
    #: split into 2+ clauses.
    items_split: int = 0
    #: Total clause units emitted across every split item (well-formed and
    #: malformed alike).
    clauses: int = 0
    #: Of ``clauses``, how many were ``malformed``.
    malformed: int = 0

    @property
    def counts(self) -> dict[str, int]:
        """Disposition tally, with every disposition present (zero or not)."""
        tally = dict.fromkeys(DISPOSITIONS, 0)
        for bullet in self.bullets:
            tally[bullet.disposition] += 1
        return tally

    @property
    def subjects_resolved(self) -> int:
        """Bullets whose subject resolved to a company/person page.

        Deliberately independent of the disposition: the host AC gates the
        apply on this count matching a previously measured one, and a bullet
        can resolve and still be ``no-source`` or ``ambiguous-source``.
        """
        return sum(1 for b in self.bullets if b.uid)

    @property
    def subjects_unresolved(self) -> int:
        return len(self.bullets) - self.subjects_resolved

    def _bullet_dict(self, bullet: BulletPlan) -> dict[str, object]:
        """*bullet* as a dict, version 1 shape unless this report is v2+.

        Built by filtering ``asdict(bullet)`` down to
        :data:`_V1_BULLET_FIELDS` (preserving their declaration order)
        rather than hand-listing them again, so the two can never drift
        apart field-for-field.
        """
        rendered = {k: v for k, v in asdict(bullet).items() if k in _V1_BULLET_FIELDS}
        if self.version >= DECOMPOSE_REPORT_VERSION_2:
            rendered["item_ordinal"] = bullet.item_ordinal
            rendered["clause_index"] = bullet.clause_index
            rendered["clause_shape"] = bullet.clause_shape
        return rendered

    def to_dict(self) -> dict[str, object]:
        out: dict[str, object] = {
            "version": self.version,
            "source_uid": self.source_uid,
            "source_name": self.source_name,
            "source_path": self.source_path,
            "subject_until": self.subject_until,
            "bullet_count": len(self.bullets),
            "orphan_definitions": self.orphan_definitions,
            "conflicting_labels": self.conflicting_labels,
            "counts": self.counts,
            "subjects": {
                "resolved": self.subjects_resolved,
                "unresolved": self.subjects_unresolved,
            },
        }
        if self.version >= DECOMPOSE_REPORT_VERSION_2:
            out["items"] = self.items
            out["clause_split"] = {
                "items_split": self.items_split,
                "clauses": self.clauses,
                "malformed": self.malformed,
            }
        out["bullets"] = [self._bullet_dict(b) for b in self.bullets]
        return out


@dataclass
class ApplyResult:
    """What ``--apply`` actually did."""

    attached: int = 0
    skipped_already_present: int = 0
    dropped: int = 0
    written_paths: list[str] = field(default_factory=list)
    source_rewritten: bool = False


# --- parsing -------------------------------------------------------------


def bullet_id(ordinal: int, raw: str) -> str:
    """The stable id for a bullet: ordinal plus a hash of its own text.

    The ordinal alone would re-key every ruling as soon as one bullet is
    removed; the hash alone would not tell an operator where to look. Both
    together survive reordering AND catch a bullet whose text changed between
    the dry-run that produced a ruling and the apply that consumes it — see
    :func:`check_resolutions`.
    """
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
    return f"{ordinal}-{digest}"


def clause_id(item_ordinal: int, clause_index: int, raw: str) -> str:
    """The stable id for a split clause (issue athenaeum#1947).

    ``<item_ordinal>.<clause_index>-<first 12 hex of sha256(raw)>`` -- the
    dot is deliberate: a version 1 id (:func:`bullet_id`) is always
    ``<int>-<hex>`` with no dot in its first segment, so a clause id can
    never collide with one. *raw* is the clause's own text (with its ``- ``
    prefix, matching :func:`bullet_id`'s convention), so editing one
    character of the clause invalidates any ruling that names it, exactly
    like a version 1 bullet.
    """
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
    return f"{item_ordinal}.{clause_index}-{digest}"


def parse_definitions(body: str) -> dict[str, list[Definition]]:
    """Map every footnote label in *body* to ALL of its definitions, in order.

    Deliberately NOT :func:`athenaeum.footnote_markers.parse_footnote_definitions`,
    which keeps only the first definition per label (how a markdown renderer
    resolves a duplicate). Conflicting definitions are the whole problem here:
    13 labels on the live page carry two or more, and silently taking the
    first would attach a fact under another company's source.
    """
    out: dict[str, list[Definition]] = {}
    for match in FOOTNOTE_DEF_RE.finditer(body):
        label = match.group(1)
        text = match.group(2).strip()
        drive = DRIVE_PATH_RE.search(text)
        out.setdefault(label, []).append(
            Definition(label=label, text=text, drive_path=drive.group(0) if drive else "")
        )
    return out


def parse_bullets(
    body: str,
) -> tuple[list[tuple[int, str, list[str]]], dict[str, list[Definition]]]:
    """Split *body* into ``(ordinal, raw_line, refs)`` bullets and definitions.

    A "bullet" is a top-level ``- `` list item. Footnote definitions start
    ``[^label]:`` and so can never be mistaken for one.
    """
    bullets: list[tuple[int, str, list[str]]] = []
    ordinal = 0
    for line in body.splitlines():
        match = BULLET_RE.match(line)
        if not match:
            continue
        ordinal += 1
        bullets.append((ordinal, line.rstrip(), INLINE_MARKER_RE.findall(line)))
    return bullets, parse_definitions(body)


def extract_subject(raw: str, subject_until: re.Pattern[str]) -> str:
    """The bullet's leading span, up to the first *subject_until* match.

    *raw* is the whole bullet line including its ``- `` marker, which is
    stripped first. A bullet the regex does not match yields ``""`` — the
    caller turns that into ``unresolved``. There is deliberately NO fallback:
    scanning index keys for the longest prefix of the bullet would attach
    "X Labs" to an existing shorter entity "X", which is exactly the silent
    misattribution this pass must not make.
    """
    match = BULLET_RE.match(raw)
    text = match.group(1) if match else raw
    boundary = subject_until.search(text)
    if not boundary:
        return ""
    return text[: boundary.start()].strip()


def split_into_clauses(text: str) -> list[str]:
    """Split one list item's *text* into clauses by the terminator rule
    (issue athenaeum#1947).

    *text* is the item's text with its leading ``- `` already stripped (as
    returned by :data:`BULLET_RE`'s capture group).

    A terminator is a run of one or more inline footnote markers
    (:data:`CLAUSE_MARKER_RUN_RE`), optionally followed by one of
    ``.``/``;``/``,``, and then either whitespace or the end of *text*. A
    marker run that does NOT satisfy that -- most commonly one glued
    directly to the next clause's text with no separator at all, the
    athenaeum#1942 interleave shape -- is not a terminator and is simply
    swallowed into whichever clause eventually closes; this is what turns
    an interleave into one ``malformed`` clause spanning two subjects rather
    than a silent mis-split.

    A clause is the span from the end of the previous terminator up to and
    including THIS marker run; the optional trailing punctuation and the
    whitespace after it are the separator and belong to neither clause.
    Text after the last terminator becomes a final clause (which may carry
    no marker at all).

    Always returns at least one clause (the whole of *text*, when it
    contains no terminator). The caller decides eligibility -- this
    function does not: it draws the boundaries the rule defines and nothing
    more.
    """
    clauses: list[str] = []
    prev_end = 0
    for match in CLAUSE_MARKER_RUN_RE.finditer(text):
        run_end = match.end()
        after = run_end
        if after < len(text) and text[after] in _CLAUSE_TERMINATOR_PUNCT:
            after += 1
        if after < len(text) and not text[after].isspace():
            continue  # not a terminator -- this run stays inside its clause
        clauses.append(text[prev_end:run_end])
        sep_end = after
        while sep_end < len(text) and text[sep_end].isspace():
            sep_end += 1
        prev_end = sep_end
    if prev_end < len(text) or not clauses:
        clauses.append(text[prev_end:])
    return clauses


# --- resolution ----------------------------------------------------------


def resolve_subject(subject: str, index: EntityIndex, self_uid: str) -> tuple[str, str, str]:
    """Resolve *subject* to ``(uid, page_filename, note)``.

    Order is pinned, and each step is strictly narrower than a guess:

    1. :meth:`athenaeum.models.EntityIndex.lookup` — exact name or alias,
       case-insensitive — restricted to :data:`RESOLVABLE_TYPES` and
       excluding the source page's own uid (an aggregate page names itself).
    2. A loose pass over the index via
       :func:`athenaeum.entity_resolution.normalize_name`, accepted ONLY when
       it yields exactly one distinct uid. Two loose candidates is
       ``unresolved``, never a coin flip.

    Returns ``("", "", note)`` when neither step resolves.
    """
    if not subject:
        return "", "", "no subject (the bullet did not match --subject-until)"

    entry = index.lookup(subject)
    if entry is not None and entry.uid != self_uid and (entry.type or "") in RESOLVABLE_TYPES:
        return entry.uid, entry.path.name, ""

    wanted = normalize_name(subject)
    loose: dict[str, str] = {}
    for key, candidate in index.items():
        if candidate.uid == self_uid or (candidate.type or "") not in RESOLVABLE_TYPES:
            continue
        if normalize_name(key) == wanted:
            loose[candidate.uid] = candidate.path.name
    if len(loose) == 1:
        uid, name = next(iter(loose.items()))
        return uid, name, ""
    if len(loose) > 1:
        return "", "", f"{len(loose)} loose candidates — refusing to guess"
    return "", "", "no company/person page matches this subject"


def select_definition(
    refs: list[str],
    subject: str,
    definitions: dict[str, list[Definition]],
    *,
    collision_subjects: dict[str, frozenset[str]] | None = None,
) -> tuple[Definition | None, str]:
    """Pick the ONE definition a bullet's fact should carry with it.

    A label with exactly one definition is taken as-is. A label with two or
    more conflicting definitions is resolved by the slug rule: the definition
    whose ``drive/<id>-<slug>.md`` slug equals ``slugify(subject)`` wins, and
    only if exactly one does. Anything else returns ``(None, reason)`` so the
    caller can mark the bullet ``ambiguous-source`` rather than attach a fact
    under a source that may belong to a different company.

    *collision_subjects* is the ``--split-clauses``-only guard (issue
    athenaeum#1947): ``{label: {subjects referencing it page-wide}}``, built
    by the caller over every emitted unit's OWN subject (never the whole
    item's). ``None`` (the default, and always the case for a version 1
    report) disables the guard entirely, so a caller that never passes it
    gets exactly today's behaviour. When supplied, a label whose set has 2+
    DISTINCT subjects is no longer taken as-is even with a single
    definition: that definition is accepted only when its drive slug equals
    THIS clause's own subject slug. A label with 0 or 1 subjects (every
    legacy case; a label used only within one item) is unaffected, because
    the collision the guard exists to catch has not happened.
    """
    candidates: list[Definition] = []
    for label in refs:
        candidates.extend(definitions.get(label, []))
    if not candidates:
        return None, "no definition for this bullet's label(s)"
    if len(candidates) == 1:
        definition = candidates[0]
        if collision_subjects is not None:
            subjects = collision_subjects.get(definition.label, frozenset())
            if len(subjects) > 1:
                wanted = slugify(subject)
                if not wanted or definition.slug != wanted:
                    return None, (
                        f"{len(subjects)} subjects share label {definition.label!r} "
                        "page-wide; this clause's drive slug does not match"
                    )
        return definition, ""

    wanted = slugify(subject)
    matched = [d for d in candidates if wanted and d.slug == wanted]
    if len(matched) == 1:
        return matched[0], ""
    if not matched:
        return None, f"{len(candidates)} conflicting definitions; no drive slug matches the subject"
    return None, f"{len(matched)} conflicting definitions share the subject's drive slug"


def already_present(raw: str, definition: Definition | None, target_body: str) -> bool:
    """True when *target_body* demonstrably already carries this bullet's fact.

    A strict CONJUNCTION of two independent signals, because this predicate's
    only effect is to DROP a fact as a duplicate:

    1. the target already cites the bullet's ``drive/`` source path, and
    2. the bullet has at least one double-quoted span and the target already
       carries every one of them.

    A bullet with no quoted span is therefore never ``already-present``: one
    shared source file is not evidence that this particular sentence already
    landed, and erring the other way loses a fact silently.
    """
    if definition is None or not definition.drive_path:
        return False
    if definition.drive_path not in target_body:
        return False
    quoted = QUOTED_RE.findall(raw)
    if not quoted:
        return False
    return all(q in target_body for q in quoted)


# --- report --------------------------------------------------------------


def _read_page(path: Path) -> tuple[dict[str, object], str]:
    return parse_frontmatter(path.read_text(encoding="utf-8"))


def _classify_unit(
    *,
    raw: str,
    subject: str,
    refs: list[str],
    definitions: dict[str, list[Definition]],
    idx: EntityIndex,
    source_uid: str,
    collision_subjects: dict[str, frozenset[str]] | None = None,
) -> tuple[str, str, str, str, str, str]:
    """Classify one unit -- a whole item or a well-formed clause.

    Pulled out of the original per-bullet loop so the version 1 whole-item
    path (called with ``collision_subjects=None``) and the
    ``split_clauses`` paths (an unsplit item, or a well-formed clause) all
    decide a disposition the exact same way. Deliberately does not touch
    ``ordinal``/``id``/``item_ordinal``/``clause_index``/``clause_shape`` --
    the caller owns those, since they differ by which of the three callers
    this is. A ``malformed`` clause never reaches this function at all: it
    is forced ``unresolved`` with an empty subject by its caller directly.

    Returns ``(disposition, uid, target, source, source_drive_path, note)``.
    """
    uid, target, resolve_note = resolve_subject(subject, idx, source_uid)

    # Order matters and is deliberate: a bullet with no source can never
    # be attached whatever its subject resolves to, so `no-source` is
    # decided first; a bullet whose source cannot be picked cannot be
    # compared against a target either, so `ambiguous-source` precedes
    # the already-present check.
    # The definition is selected even when the subject did NOT resolve:
    # an `unresolved` bullet the operator later rules to a uid must still
    # carry its own footnote across, so the selection cannot wait on the
    # subject. It is recorded on the plan either way.
    definition, why = (
        (None, "")
        if not refs
        else select_definition(refs, subject, definitions, collision_subjects=collision_subjects)
    )
    source = definition.text if definition is not None else ""
    source_drive_path = definition.drive_path if definition is not None else ""

    if not refs:
        note = "bullet carries no footnote reference"
        return "no-source", uid, target, source, source_drive_path, note
    if not uid:
        return "unresolved", uid, target, source, source_drive_path, resolve_note
    if definition is None:
        return "ambiguous-source", uid, target, source, source_drive_path, why
    target_path = idx.get_by_uid(uid)
    target_body = _read_page(target_path)[1] if target_path else ""
    if already_present(raw, definition, target_body):
        note = "target already cites this source and carries its quoted span(s)"
        return "already-present", uid, target, source, source_drive_path, note
    return "attached", uid, target, source, source_drive_path, ""


@dataclass
class _PendingUnit:
    """One ``split_clauses`` unit after pass 1, before definition selection.

    Internal to :func:`build_report`'s split path -- never returned to a
    caller. Split out of pass 1 (subjects, shapes, eligibility) from pass 3
    (disposition) because pass 2 (:func:`build_report`'s
    ``collision_subjects`` map) must see every unit's subject before ANY
    unit selects a definition.
    """

    ordinal: int
    raw: str
    subject: str
    refs: list[str]
    item_ordinal: int
    clause_index: int
    clause_shape: str
    id: str
    #: Only meaningful when ``clause_shape == "malformed"``.
    malformed_matches: int = 0


def build_report(
    wiki_root: Path,
    source_uid: str,
    *,
    subject_until: str,
    index: EntityIndex | None = None,
    split_clauses: bool = False,
) -> DecomposeReport:
    """Classify every bullet on the page *source_uid* names. Writes nothing.

    Without ``split_clauses`` (the default), this is exactly version 1:
    one unit per top-level list item, and the report is
    :data:`DECOMPOSE_REPORT_VERSION`.

    With ``split_clauses=True`` (issue athenaeum#1947), an item eligible
    under :func:`split_into_clauses`'s rule (2+ clauses AND 2+
    ``subject_until`` matches) is split into one unit per clause; an
    ineligible item is still emitted whole, with its version 1 id, exactly
    as it would be without the flag. The report is
    :data:`DECOMPOSE_REPORT_VERSION_2`.
    """
    idx = index if index is not None else EntityIndex(wiki_root)
    source_path = idx.get_by_uid(source_uid)
    if source_path is None:
        raise DecomposeError(f"{source_uid}: no wiki page carries this uid")
    meta, body = _read_page(source_path)
    pattern = re.compile(subject_until)

    raw_bullets, definitions = parse_bullets(body)
    referenced = {label for _, _, refs in raw_bullets for label in refs}
    report = DecomposeReport(
        source_uid=source_uid,
        source_name=str(meta.get("name", "")),
        source_path=source_path.name,
        subject_until=subject_until,
        orphan_definitions=sum(1 for label in definitions if label not in referenced),
        conflicting_labels=sum(1 for defs in definitions.values() if len(defs) > 1),
        version=DECOMPOSE_REPORT_VERSION_2 if split_clauses else DECOMPOSE_REPORT_VERSION,
    )

    if not split_clauses:
        for ordinal, raw, refs in raw_bullets:
            subject = extract_subject(raw, pattern)
            disposition, uid, target, source, source_drive_path, note = _classify_unit(
                raw=raw,
                subject=subject,
                refs=refs,
                definitions=definitions,
                idx=idx,
                source_uid=source_uid,
            )
            report.bullets.append(
                BulletPlan(
                    ordinal=ordinal,
                    id=bullet_id(ordinal, raw),
                    raw=raw,
                    subject=subject,
                    refs=list(refs),
                    disposition=disposition,
                    uid=uid,
                    target=target,
                    source=source,
                    source_drive_path=source_drive_path,
                    note=note,
                )
            )
        return report

    report.items = len(raw_bullets)

    # Pass 1: decide, per item, whether it splits -- and if so, every
    # clause's own text/subject/refs/shape.
    pending: list[_PendingUnit] = []
    for ordinal, raw, item_refs in raw_bullets:
        item_match = BULLET_RE.match(raw)
        item_text = item_match.group(1) if item_match else raw
        clause_texts = split_into_clauses(item_text)
        item_matches = len(pattern.findall(item_text))
        if len(clause_texts) < 2 or item_matches < 2:
            pending.append(
                _PendingUnit(
                    ordinal=ordinal,
                    raw=raw,
                    subject=extract_subject(raw, pattern),
                    refs=list(item_refs),
                    item_ordinal=ordinal,
                    clause_index=0,
                    clause_shape="item",
                    id=bullet_id(ordinal, raw),
                )
            )
            continue
        report.items_split += 1
        for clause_index, clause_text in enumerate(clause_texts, start=1):
            report.clauses += 1
            clause_raw = f"- {clause_text}"
            matches = len(pattern.findall(clause_text))
            clause_refs = INLINE_MARKER_RE.findall(clause_text)
            if matches != 1:
                report.malformed += 1
                pending.append(
                    _PendingUnit(
                        ordinal=ordinal,
                        raw=clause_raw,
                        subject="",
                        refs=clause_refs,
                        item_ordinal=ordinal,
                        clause_index=clause_index,
                        clause_shape="malformed",
                        id=clause_id(ordinal, clause_index, clause_raw),
                        malformed_matches=matches,
                    )
                )
                continue
            pending.append(
                _PendingUnit(
                    ordinal=ordinal,
                    raw=clause_raw,
                    subject=extract_subject(clause_raw, pattern),
                    refs=clause_refs,
                    item_ordinal=ordinal,
                    clause_index=clause_index,
                    clause_shape="clause",
                    id=clause_id(ordinal, clause_index, clause_raw),
                )
            )

    # Pass 2: the page-wide label -> {subjects} map the collision guard in
    # `select_definition` needs (see its docstring). A unit with no subject
    # (malformed) contributes nothing -- it never carries a selectable
    # source regardless.
    label_subjects: dict[str, set[str]] = {}
    for unit in pending:
        if not unit.subject:
            continue
        for label in unit.refs:
            label_subjects.setdefault(label, set()).add(unit.subject)
    collision_subjects = {k: frozenset(v) for k, v in label_subjects.items()}

    # Pass 3: classify every unit now that collision_subjects is complete.
    for unit in pending:
        if unit.clause_shape == "malformed":
            report.bullets.append(
                BulletPlan(
                    ordinal=unit.ordinal,
                    id=unit.id,
                    raw=unit.raw,
                    subject="",
                    refs=unit.refs,
                    disposition="unresolved",
                    note=(
                        "malformed clause: --subject-until matched "
                        f"{unit.malformed_matches} time(s)"
                    ),
                    item_ordinal=unit.item_ordinal,
                    clause_index=unit.clause_index,
                    clause_shape="malformed",
                )
            )
            continue
        disposition, uid, target, source, source_drive_path, note = _classify_unit(
            raw=unit.raw,
            subject=unit.subject,
            refs=unit.refs,
            definitions=definitions,
            idx=idx,
            source_uid=source_uid,
            collision_subjects=collision_subjects,
        )
        report.bullets.append(
            BulletPlan(
                ordinal=unit.ordinal,
                id=unit.id,
                raw=unit.raw,
                subject=unit.subject,
                refs=list(unit.refs),
                disposition=disposition,
                uid=uid,
                target=target,
                source=source,
                source_drive_path=source_drive_path,
                note=note,
                item_ordinal=unit.item_ordinal,
                clause_index=unit.clause_index,
                clause_shape=unit.clause_shape,
            )
        )

    return report


def write_report(report: DecomposeReport, path: Path) -> None:
    """Serialize *report* to *path* as JSON.

    The report names subjects and page filenames, so it goes to a host path
    the operator reads directly. Nothing in this module prints it.
    """
    atomic_write_text(path, json.dumps(report.to_dict(), indent=2, sort_keys=False) + "\n")


# --- apply ---------------------------------------------------------------


def load_resolutions(path: Path) -> dict[str, str]:
    """Read the operator's rulings: ``{bullet-id: uid | "drop"}``."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise DecomposeError(f"{path}: not valid JSON: {exc}") from exc
    if not isinstance(raw, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in raw.items()
    ):
        raise DecomposeError(f"{path}: expected an object of bullet-id -> uid|'drop'")
    return raw


def check_resolutions(report: DecomposeReport, resolutions: dict[str, str]) -> list[str]:
    """Return every reason ``--apply`` must refuse, or an empty list.

    Three refusals, all of them "the operator's ruling and the page no longer
    agree" in different disguises:

    * a blocking bullet with no ruling at all;
    * a ruling whose id names no bullet on the page — which, because the id
      embeds a hash of the bullet's own text, is also how a ruling written
      against a since-edited bullet surfaces;
    * a ruling that names a uid for a bullet whose source could not be
      picked (``no-source``/``ambiguous-source``). Attaching there would move
      the fact without its provenance, so only ``drop`` is meaningful.
    """
    by_id = {b.id: b for b in report.bullets}
    problems: list[str] = []
    for bullet in report.bullets:
        if bullet.disposition in BLOCKING_DISPOSITIONS and bullet.id not in resolutions:
            problems.append(f"{bullet.id}: {bullet.disposition} and no ruling in --resolutions")
    for ruling_id, verdict in sorted(resolutions.items()):
        ruled = by_id.get(ruling_id)
        if ruled is None:
            problems.append(
                f"{ruling_id}: ruling does not match any bullet on the page "
                "(the bullet's text changed, or the id is wrong)"
            )
            continue
        if verdict != "drop" and not ruled.source:
            problems.append(
                f"{ruling_id}: ruled to a uid but the bullet has no selectable source "
                f"({ruled.disposition}) — only 'drop' can resolve it"
            )
    return problems


def _next_numeric_label(target_body: str) -> str:
    """The first integer label free on *target_body*, as a string.

    Scans DEFINITIONS and inline REFS alike: a dangling ref with no
    definition still occupies its label, and reusing it would silently
    re-point an existing marker at the incoming fact.
    """
    used: set[str] = {m.group(1) for m in FOOTNOTE_DEF_RE.finditer(target_body)}
    used |= set(INLINE_MARKER_RE.findall(target_body))
    highest = 0
    for label in used:
        if label.isdigit():
            highest = max(highest, int(label))
    candidate = highest + 1
    while str(candidate) in used:
        candidate += 1
    return str(candidate)


def _renumber(raw: str, new_label: str) -> str:
    """Rewrite every inline marker in *raw* to ``[^new_label]``.

    A bullet carries exactly one fact and therefore attaches exactly one
    definition, so every marker on it resolves to that same definition once
    it lands on the target.
    """
    return INLINE_MARKER_RE.sub(f"[^{new_label}]", raw)


def _insert_fact(target_body: str, bullet_line: str, definition_line: str) -> str:
    """Place *bullet_line* and *definition_line* into *target_body*.

    The bullet goes immediately BEFORE the first footnote-definition line
    (so it reads as the last item of the page's prose, not as part of the
    bibliography) and the definition goes immediately after the LAST one (so
    the bibliography stays one contiguous block). A target with no footnote
    definitions at all gets both appended, separated by a blank line.
    """
    lines = target_body.split("\n")
    def_indices = [i for i, line in enumerate(lines) if FOOTNOTE_DEF_RE.match(line)]
    if not def_indices:
        while lines and not lines[-1].strip():
            lines.pop()
        lines.extend(["", bullet_line, "", definition_line, ""])
        return "\n".join(lines)
    first, last = def_indices[0], def_indices[-1]
    lines[first:first] = [bullet_line, ""]
    lines.insert(last + 3, definition_line)
    return "\n".join(lines)


def _validation_error_shape(exc: ValidationError) -> str:
    """A content-free description of *exc*: field path + error type only.

    Never ``str(exc)`` — pydantic embeds the offending ``input_value`` in its
    own message, which for frontmatter would leak corpus content into a CLI
    error or exception string.
    """
    shapes = [f"{'.'.join(str(p) for p in e['loc'])}: {e['type']}" for e in exc.errors()]
    return ", ".join(shapes) or "invalid frontmatter"


def _bump_and_render(meta: dict[str, object], body: str, today: str) -> str:
    """Validate *meta*, bump ``updated``, and render the whole page."""
    meta["updated"] = today
    validate_wiki_meta(dict(meta))
    return render_frontmatter(meta) + "\n" + body


def _attach_to_target(
    target_path: Path, plan: BulletPlan, definition_text: str, today: str
) -> bool:
    """Attach one bullet to one target page. Returns True when it wrote.

    The target is re-read HERE, at write time, rather than trusted from the
    report: a target that gained this fact between the dry-run and the apply
    (or on a previous ``--apply`` of the same inputs) must not gain it twice.
    That re-check is what makes a second apply a byte-for-byte no-op.
    """
    meta, body = _read_page(target_path)
    drive = DRIVE_PATH_RE.search(definition_text)
    definition = Definition(
        label="", text=definition_text, drive_path=drive.group(0) if drive else ""
    )
    if already_present(plan.raw, definition, body):
        return False

    label = _next_numeric_label(body)
    new_body = _insert_fact(
        body, _renumber(plan.raw, label), f"[^{label}]: {definition_text}"
    )
    atomic_write_text(target_path, _bump_and_render(meta, new_body, today))
    return True


def validate_rewrite(
    rewrite_body: str, description: str, subjects: list[str], index: EntityIndex
) -> list[str]:
    """Return every reason the source-page rewrite must be refused.

    Three properties, each of which the rewrite exists to establish:

    * it is SHORT — under :data:`MAX_REWRITE_BODY_BYTES`, because an
      aggregate that merely got shorter is still an aggregate;
    * every page it links to exists — a pointer to where the history now
      lives is the whole value of the rewrite, and a dead one is worse than
      none;
    * no bullet subject appears anywhere in it, frontmatter ``description:``
      included. That last one is the real test of the decomposition: a page
      that still lists the companies has not stopped being about them.
    """
    problems: list[str] = []
    size = len(rewrite_body.encode("utf-8"))
    if size > MAX_REWRITE_BODY_BYTES:
        problems.append(f"rewrite body is {size} bytes, over the {MAX_REWRITE_BODY_BYTES} limit")

    for name in WIKILINK_RE.findall(rewrite_body):
        if index.lookup(name.strip()) is None:
            problems.append(f"rewrite body links to [[{name.strip()}]], which is not a wiki page")
    for rel in MD_LINK_RE.findall(rewrite_body):
        if not (index.wiki_root / rel).exists():
            problems.append(f"rewrite body links to {rel}, which is not a file in the wiki")

    haystack = f"{description}\n{rewrite_body}".casefold()
    for subject in sorted({s for s in subjects if s}):
        if subject.casefold() in haystack:
            problems.append(f"rewrite still names a decomposed subject: {subject!r}")
    return problems


def apply_report(
    report: DecomposeReport,
    wiki_root: Path,
    *,
    resolutions: dict[str, str],
    rewrite_body: str,
    description: str,
    index: EntityIndex | None = None,
    today: str | None = None,
) -> ApplyResult:
    """Attach every ruled-and-resolved fact, then rewrite the source page.

    Refuses — raising :class:`DecomposeError` before ANY write — when a
    blocking bullet is unruled, a ruling no longer matches its bullet, a
    ruled uid names no page, or the rewrite fails
    :func:`validate_rewrite`. Checking everything up front is what makes
    "nothing was written" a true statement after a refusal.
    """
    idx = index if index is not None else EntityIndex(wiki_root)
    stamp = today if today is not None else date.today().isoformat()

    # A page with no bullets left is already decomposed: there is nothing to
    # rule on, so rulings left over from the run that decomposed it are inert
    # rather than stale-and-refused. This is what makes a second `--apply`
    # with the SAME inputs exit clean instead of erroring on ids that no
    # longer name anything.
    problems = check_resolutions(report, resolutions) if report.bullets else []
    problems.extend(
        validate_rewrite(rewrite_body, description, [b.subject for b in report.bullets], idx)
    )
    plans: list[tuple[BulletPlan, Path]] = []
    for bullet in report.bullets:
        ruling = resolutions.get(bullet.id)
        if ruling == "drop":
            continue
        uid = ruling if ruling else bullet.uid
        if bullet.disposition == "already-present" and not ruling:
            continue
        if not uid or not bullet.source:
            continue
        target_path = idx.get_by_uid(uid)
        if target_path is None:
            problems.append(f"{bullet.id}: ruled to uid {uid}, which names no wiki page")
            continue
        plans.append((bullet, target_path))

    source_path = idx.get_by_uid(report.source_uid)
    if source_path is None:
        problems.append(f"{report.source_uid}: no wiki page carries this uid")

    # Pre-pass: validate every page this apply would write, BEFORE any write
    # happens. apply_report -> _attach_to_target -> _bump_and_render ->
    # validate_wiki_meta can otherwise raise pydantic.ValidationError mid-run,
    # after some targets are already written — this makes the whole apply
    # all-or-nothing per run instead of partially-applied. The report names
    # each failing page and its error SHAPE only (field path + pydantic error
    # type) — never ``str(exc)``, whose ``input_value=`` dump would echo the
    # page's own frontmatter content into a CLI error / exception message.
    invalid_pages: list[str] = []
    checked_targets: set[Path] = set()
    for _, target_path in plans:
        if target_path in checked_targets:
            continue
        checked_targets.add(target_path)
        target_meta, _ = _read_page(target_path)
        try:
            _bump_and_render(dict(target_meta), "", stamp)
        except ValidationError as exc:
            invalid_pages.append(f"{target_path.name} ({_validation_error_shape(exc)})")
    if source_path is not None:
        source_meta, _ = _read_page(source_path)
        rewritten_meta = dict(source_meta)
        rewritten_meta["description"] = description
        try:
            _bump_and_render(rewritten_meta, rewrite_body, stamp)
        except ValidationError as exc:
            invalid_pages.append(f"{source_path.name} ({_validation_error_shape(exc)})")
    if invalid_pages:
        problems.append(
            f"{len(invalid_pages)} page(s) would fail frontmatter validation: "
            + "; ".join(invalid_pages)
        )

    if problems:
        raise DecomposeError("; ".join(problems))

    result = ApplyResult(
        # Count only resolutions for bullets in THIS report: a resolutions
        # file re-run against an already-decomposed page (empty bullets)
        # must not double-count drops a previous apply already resolved.
        dropped=sum(1 for b in report.bullets if resolutions.get(b.id) == "drop"),
        skipped_already_present=sum(
            1
            for b in report.bullets
            if b.disposition == "already-present" and b.id not in resolutions
        ),
    )
    for bullet, target_path in plans:
        if _attach_to_target(target_path, bullet, bullet.source, stamp):
            result.attached += 1
            result.written_paths.append(target_path.name)
        else:
            result.skipped_already_present += 1

    if source_path is None:  # pragma: no cover - proved not None in the pre-pass above
        raise DecomposeError(f"{report.source_uid}: no wiki page carries this uid")
    meta, _ = _read_page(source_path)
    rewritten = dict(meta)
    rewritten["description"] = description
    new_text = _bump_and_render(rewritten, rewrite_body, stamp)
    if new_text != source_path.read_text(encoding="utf-8"):
        atomic_write_text(source_path, new_text)
        result.source_rewritten = True
        result.written_paths.append(source_path.name)

    log.info(
        "decompose-page %s: attached=%d already-present=%d dropped=%d",
        report.source_uid,
        result.attached,
        result.skipped_already_present,
        result.dropped,
    )
    return result


__all__ = [
    "BLOCKING_DISPOSITIONS",
    "CLAUSE_MARKER_RUN_RE",
    "CLAUSE_SHAPES",
    "DECOMPOSE_REPORT_VERSION",
    "DECOMPOSE_REPORT_VERSION_2",
    "DISPOSITIONS",
    "MAX_REWRITE_BODY_BYTES",
    "RESOLVABLE_TYPES",
    "ApplyResult",
    "BulletPlan",
    "DecomposeError",
    "DecomposeReport",
    "Definition",
    "already_present",
    "apply_report",
    "build_report",
    "bullet_id",
    "check_resolutions",
    "clause_id",
    "extract_subject",
    "load_resolutions",
    "parse_bullets",
    "parse_definitions",
    "resolve_subject",
    "select_definition",
    "split_into_clauses",
    "validate_rewrite",
    "write_report",
]
