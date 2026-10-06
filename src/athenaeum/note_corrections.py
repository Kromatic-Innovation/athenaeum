# SPDX-License-Identifier: Apache-2.0
"""Note-level move/drop correction pass for Notes lines on person pages
(issue athenaeum#1976).

**The problem.** A person page named by first name only acts as the tier-1
registry match for every bare mention of that name and accumulates Notes
lines about OTHER people (the athenaeum#1216 match-magnet pattern). No governed
path can remove or relocate an individual Notes line once it has landed:

* :mod:`athenaeum.page_decompose` resolves a line's target through its
  footnote citation, refuses an uncited line outright (``no-source``), and
  its apply path requires a full ``--rewrite-body`` capped at 2,048 bytes
  (the wrong contract for a page that keeps its own content — the point of
  this pass is that the page stays, only the misfiled lines move).
* The field-correction fast path (:mod:`athenaeum.corrections`) corrects
  frontmatter fields only; a body-edit record in ``raw/`` would make write
  access to ``raw/`` a path to deleting wiki content, which
  ``docs/design/field-corrections.md`` section 12a names as exactly the
  trust boundary that must not move.

So this module is a deterministic CLI pass in the shape
:mod:`athenaeum.page_decompose` already established for the same reason
(``docs/design/note-corrections.md`` has the fuller rationale): read a
batch, resolve every id against ONE snapshot of the page, write only when
every record checks out, and never call a model. The CLI half
(:mod:`athenaeum._cmd_correct_notes`) acquires the SAME run lock
``_cmd_decompose_page.py`` does, via the shared
:func:`athenaeum._cli_shared._acquire_or_exit` helper — this module is
athenaeum code acting AS the librarian under that lock, not a source
writing the store.

**Reused from** :mod:`athenaeum.page_decompose` **rather than re-derived**:
:func:`~athenaeum.page_decompose.parse_bullets` and
:func:`~athenaeum.page_decompose.bullet_id` for the stable per-bullet id
(``<ordinal>-<sha12>``, computed against one body snapshot so a batch's ids
never silently re-key mid-pass);
:func:`~athenaeum.page_decompose.parse_definitions` and
:data:`~athenaeum.page_decompose.BULLET_RE` for locating bullets and their
footnotes; :func:`~athenaeum.page_decompose._insert_fact`,
:func:`~athenaeum.page_decompose._next_numeric_label` and
:func:`~athenaeum.page_decompose._renumber` for attaching a moved line (and
its footnote) to a target page in the SAME layout
``_attach_to_target`` already uses; and
:func:`~athenaeum.page_decompose._bump_and_render` for the
validate-then-render step every write goes through. ``BULLET_RE`` numbers
top-level bullets across the WHOLE page, not only under a ``## Notes``
heading — this module does not special-case the heading either, for the
same reason.

**What this module refuses to guess.** Three failure classes, all refusing
the WHOLE batch before any write (the single-snapshot rule below is what
makes that possible):

* an unknown bullet id — the id no longer matches any bullet on the current
  snapshot (a typo, or a batch authored against a stale copy of the page);
* a ``move`` record's ``target_uid`` names no wiki page;
* a ``move`` record's ``target_uid`` resolves to the SOURCE page itself —
  meaningless as an operation, and letting it through would make the
  source page a write target of its own apply (see :func:`apply_batch`'s
  commit-phase comment for exactly how that would silently destroy the
  moved line).

**All-or-nothing, one snapshot.** Every bullet id in a batch is resolved
against the SAME read of the source page — :func:`apply_batch` parses the
page exactly once, validates every record against that parse, and only
then writes. A batch that drops bullet 2 and moves bullet 5 does not
re-parse between the two: bullet 5's id was computed, and is resolved,
against the ORIGINAL ordinal 5, never a renumbered 4.

**Idempotency is keyed on ``batch_id``, not on bullet content.** A bullet
that was moved or dropped on a prior apply is, by construction, gone from
the page — resolving its id against the NEW snapshot would read as
"unknown id" and wrongly refuse a harmless replay. Instead, before touching
the page at all, :func:`apply_batch` (and :func:`dry_run_report`) check
:func:`previously_applied` against ``wiki/_note_corrections_applied.jsonl``
for this exact ``batch_id`` — the SAME shape
:func:`athenaeum.corrections.previously_handed_off_correction_ids` already
uses for its own idempotency check. A hit makes the WHOLE batch a no-op:
every record reports disposition ``noop``, nothing is read from or written
to any page, and no second ledger line is appended.

**No LLM call anywhere in this module.** Every operation here is text
arithmetic over an already-compiled page plus a JSON batch file — nothing
here resolves a name, classifies a fact, or drafts prose. See
``tests/test_note_corrections.py``'s subprocess-isolated proof.

Layering: L4 domain/pipeline — same layer as
:mod:`athenaeum.page_decompose`, which it imports (same-layer imports are
allowed; see that module's own docstring for the identical claim about
:mod:`athenaeum.entity_resolution`). Everything else it reads is at or
below that: :mod:`athenaeum.footnote_markers` (L1), :mod:`athenaeum.models`
(L1), :mod:`athenaeum.store` (L1) and :mod:`athenaeum.atomic_io` (L0).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from athenaeum.atomic_io import atomic_write_text
from athenaeum.footnote_markers import FOOTNOTE_DEF_RE, INLINE_MARKER_RE
from athenaeum.models import EntityIndex
from athenaeum.page_decompose import (
    BULLET_RE,
    Definition,
    _bump_and_render,
    _insert_fact,
    _next_numeric_label,
    _read_page,
    _renumber,
    bullet_id,
    parse_bullets,
    parse_definitions,
)
from athenaeum.store import append_line_durable

#: Filename this pass's ledger lives under, in the wiki root (issue athenaeum#1976).
NOTE_CORRECTIONS_LEDGER_FILENAME = "_note_corrections_applied.jsonl"

#: The two actions a batch record may carry.
MOVE = "move"
DROP = "drop"
ACTIONS: frozenset[str] = frozenset({MOVE, DROP})

#: Closed disposition set a ledger line's ``dispositions`` counts partition
#: into. ``noop`` is batch-wide (a replayed ``batch_id``, see the module
#: docstring) rather than per-record content; the two ``refused-*`` values
#: only ever appear in a ``--dry-run`` report, since a refused apply writes
#: nothing — including the ledger (see :func:`apply_batch`).
DISPOSITIONS: tuple[str, ...] = (
    "moved",
    "dropped",
    "noop",
    "refused-unknown-id",
    "refused-unknown-target",
)


class NoteCorrectionError(Exception):
    """A refusal that must abort before anything is written."""


@dataclass(frozen=True)
class CorrectionRecord:
    """One line of a batch: move one bullet to another page, or drop it."""

    bullet_id: str
    action: str
    #: Required for ``move``, forbidden for ``drop`` — see :func:`load_batch`.
    target_uid: str = ""
    #: Free-text operator annotation. Never rendered into page content or
    #: the transport footnote — purely a batch-authoring aid.
    note: str = ""


@dataclass(frozen=True)
class BatchEnvelope:
    """A parsed, structurally-valid batch file (issue athenaeum#1976)."""

    source_uid: str
    batch_id: str
    created_at: str
    records: tuple[CorrectionRecord, ...]


@dataclass
class RecordResult:
    """One record's fate: a value from :data:`DISPOSITIONS`."""

    bullet_id: str
    disposition: str


@dataclass
class CorrectionOutcome:
    """What :func:`apply_batch` did, or would have done on a replay."""

    batch_id: str
    records_total: int
    results: list[RecordResult] = field(default_factory=list)
    body_chars_before: int = 0
    body_chars_after: int = 0
    #: True when this call was a no-op replay of an already-applied
    #: ``batch_id`` — nothing was read from or written to any page.
    replay: bool = False


@dataclass
class DryRunReport:
    """``--dry-run``'s counts-only report (AC4) — never line content."""

    batch_id: str
    records_total: int
    moved: int
    dropped: int
    refused_unknown_id: int
    refused_unknown_target: int
    replay: bool
    body_chars_before: int
    body_chars_after: int
    page_size_threshold_chars: int
    file_bytes_before: int
    file_bytes_after: int
    page_flag_bytes: int


# --- batch parsing ---------------------------------------------------------


def load_batch(path: Path) -> BatchEnvelope:
    """Parse and structurally validate a batch file.

    Input shape (issue athenaeum#1976 design): ``{source_uid, batch_id,
    created_at, records: [{bullet_id, action, target_uid?, note?}]}``. A
    host-path JSON file — never read from ``raw/`` (that boundary is why
    this pass exists at all; see the module docstring).

    Refuses outright, before any page is touched, on: a non-object batch, a
    missing required key, an ``action`` outside :data:`ACTIONS`, a
    duplicate ``bullet_id`` within one batch, a ``move`` record with no
    ``target_uid``, or a ``drop`` record that carries one anyway (a ``drop``
    naming a target is almost certainly an authoring mistake, not a
    harmless no-op field).
    """
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise NoteCorrectionError(f"reading batch failed: {exc}") from exc
    try:
        raw: Any = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise NoteCorrectionError(f"batch is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise NoteCorrectionError("batch must be a JSON object")

    missing = [k for k in ("source_uid", "batch_id", "created_at", "records") if k not in raw]
    if missing:
        raise NoteCorrectionError(f"batch is missing required key(s): {', '.join(missing)}")

    source_uid, batch_id, created_at = raw["source_uid"], raw["batch_id"], raw["created_at"]
    for key, value in (
        ("source_uid", source_uid),
        ("batch_id", batch_id),
        ("created_at", created_at),
    ):
        if not isinstance(value, str) or not value:
            raise NoteCorrectionError(f"batch {key!r} must be a non-empty string")

    raw_records = raw["records"]
    if not isinstance(raw_records, list) or not raw_records:
        raise NoteCorrectionError("batch 'records' must be a non-empty list")

    records: list[CorrectionRecord] = []
    seen: set[str] = set()
    for i, item in enumerate(raw_records):
        if not isinstance(item, dict):
            raise NoteCorrectionError(f"record {i}: must be a JSON object")
        rec_id = item.get("bullet_id")
        action = item.get("action")
        if not isinstance(rec_id, str) or not rec_id:
            raise NoteCorrectionError(f"record {i}: 'bullet_id' must be a non-empty string")
        if rec_id in seen:
            raise NoteCorrectionError(f"record {i} ({rec_id}): duplicate bullet_id in batch")
        seen.add(rec_id)
        if action not in ACTIONS:
            raise NoteCorrectionError(
                f"record {i} ({rec_id}): 'action' must be one of "
                f"{sorted(ACTIONS)}, got {action!r}"
            )
        target_uid = item.get("target_uid") or ""
        note = item.get("note") or ""
        if not isinstance(target_uid, str):
            raise NoteCorrectionError(f"record {i} ({rec_id}): 'target_uid' must be a string")
        if not isinstance(note, str):
            raise NoteCorrectionError(f"record {i} ({rec_id}): 'note' must be a string")
        if action == MOVE and not target_uid:
            raise NoteCorrectionError(
                f"record {i} ({rec_id}): action 'move' requires 'target_uid'"
            )
        if action == DROP and target_uid:
            raise NoteCorrectionError(
                f"record {i} ({rec_id}): action 'drop' must not carry 'target_uid'"
            )
        if action == MOVE and target_uid == source_uid:
            # A move onto the page the line is already on is meaningless as
            # an operation, and letting it through would make the source
            # page a write target of its OWN apply: the commit loop writes
            # the appended copy to `target_cache[source_path]`, then the
            # unconditional source write immediately overwrites it with
            # `new_source_body` (computed from the ORIGINAL body, which never
            # saw the append) — net result, the line is silently destroyed.
            # Refusing here, before the envelope is even built, means the
            # whole batch never reaches that code at all (AC5's shape).
            raise NoteCorrectionError(
                f"record {i} ({rec_id}): action 'move' target_uid "
                f"{target_uid!r} is the same as the batch's own source_uid"
            )
        records.append(
            CorrectionRecord(bullet_id=rec_id, action=action, target_uid=target_uid, note=note)
        )

    return BatchEnvelope(
        source_uid=source_uid,
        batch_id=batch_id,
        created_at=created_at,
        records=tuple(records),
    )


# --- ledger -----------------------------------------------------------------


def default_note_corrections_ledger_path(wiki_root: Path) -> Path:
    return wiki_root / NOTE_CORRECTIONS_LEDGER_FILENAME


def previously_applied(wiki_root: Path, batch_id: str) -> bool:
    """True when *batch_id* already has a ledger line (issue athenaeum#1976
    idempotency — see the module docstring).

    Mirrors :func:`athenaeum.corrections.previously_handed_off_correction_ids`:
    a missing ledger or an unreadable/malformed line is tolerated as "not
    yet applied" rather than raised, so a corrupted trailing ledger line
    (the one torn-write case :func:`athenaeum.store.append_line_durable`'s
    own docstring allows) never blocks a fresh batch.
    """
    path = default_note_corrections_ledger_path(wiki_root)
    if not path.exists():
        return False
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    rec = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                if rec.get("batch_id") == batch_id:
                    return True
    except OSError:
        return False
    return False


def build_ledger_record(outcome: CorrectionOutcome) -> dict[str, Any]:
    """Build one ``_note_corrections_applied.jsonl`` line.

    Shape borrowed from :func:`athenaeum.corrections.build_ledger_record`:
    records ``records_total`` and asserts (caller's job to fail loudly on a
    mismatch, same rationale as that function) that the dispositions
    counted actually sum to it.
    """
    counts: dict[str, int] = {}
    for result in outcome.results:
        counts[result.disposition] = counts.get(result.disposition, 0) + 1
    total_counted = sum(counts.values())
    if total_counted != outcome.records_total:
        raise AssertionError(
            f"note-corrections ledger denominator mismatch for batch "
            f"{outcome.batch_id!r}: records_total={outcome.records_total} but "
            f"dispositions summed to {total_counted}"
        )
    return {
        "batch_id": outcome.batch_id,
        "recorded_at": datetime.now().isoformat(timespec="seconds"),
        "records_total": outcome.records_total,
        "dispositions": counts,
        "body_chars_before": outcome.body_chars_before,
        "body_chars_after": outcome.body_chars_after,
    }


def append_note_corrections_ledger(wiki_root: Path, outcome: CorrectionOutcome) -> None:
    record = build_ledger_record(outcome)
    path = default_note_corrections_ledger_path(wiki_root)
    append_line_durable(path, (json.dumps(record, sort_keys=True) + "\n").encode("utf-8"))


# --- body arithmetic ---------------------------------------------------------


def _bullet_positions(body: str) -> list[int]:
    """Line indices (into ``body.split("\\n")``) of every top-level bullet,
    in the same order :func:`athenaeum.page_decompose.parse_bullets`
    enumerates them — so position ``k`` (0-based) is bullet ordinal
    ``k + 1``. Both this function and ``parse_bullets`` match the same
    :data:`~athenaeum.page_decompose.BULLET_RE` object over the body's
    lines in document order, so the two orderings can never disagree.
    """
    return [i for i, line in enumerate(body.split("\n")) if BULLET_RE.match(line)]


def _strip_bullets(body: str, ordinals: set[int]) -> str:
    """Remove the bullets at *ordinals* (1-based) and any footnote
    definition no remaining line references.

    Structural only — a caller decides move vs. drop; by the time a bullet
    reaches here it is simply gone, exactly as if the page never carried
    it. A definition is removed only when NO line anywhere in the
    resulting body still cites its label (``BULLET_RE`` numbers bullets
    page-wide, so a label shared outside the batch's own section is not
    orphaned just because this batch touched other bullets).
    """
    lines = body.split("\n")
    positions = _bullet_positions(body)
    remove_at = {positions[o - 1] for o in ordinals if 1 <= o <= len(positions)}
    kept = [line for i, line in enumerate(lines) if i not in remove_at]
    new_body = "\n".join(kept)

    referenced = set(INLINE_MARKER_RE.findall(new_body))
    out: list[str] = []
    for line in new_body.split("\n"):
        match = FOOTNOTE_DEF_RE.match(line)
        if match is not None and match.group(1) not in referenced:
            continue
        out.append(line)
    return "\n".join(out)


def _carry_footnote(raw: str, definitions: dict[str, list[Definition]]) -> tuple[str, str] | None:
    """The ONE definition this bullet's own marker(s) cite, or ``None``.

    Mirrors :func:`athenaeum.page_decompose._renumber`'s own stated
    assumption ("a bullet carries exactly one fact and therefore attaches
    exactly one definition") — a bullet naming more than one label still
    gets exactly one definition carried, keyed off the FIRST marker in
    document order. A marker with no definition anywhere on the source
    page (a dangling reference) is treated as uncited: there is no real
    source text to carry, so the caller falls back to a transport footnote
    rather than inventing one.
    """
    refs = INLINE_MARKER_RE.findall(raw)
    if not refs:
        return None
    candidates = definitions.get(refs[0], [])
    if not candidates:
        return None
    # A label with 2+ conflicting definitions has no subject/slug to
    # disambiguate against here (unlike page_decompose's select_definition,
    # which resolves a company-page conflation this pass does not have) —
    # take the first, matching parse_footnote_definitions' own
    # first-definition-wins convention elsewhere in this codebase.
    return refs[0], candidates[0].text


def _error_shape(exc: ValidationError) -> str:
    """Field path + error type only — never ``str(exc)``, whose
    ``input_value=`` dump would echo the page's own frontmatter content
    into a CLI error message (same rationale as ``apply_report``'s
    pre-pass in :mod:`athenaeum.page_decompose`)."""
    parts = [f"{'.'.join(str(p) for p in e['loc'])}:{e['type']}" for e in exc.errors()]
    return "; ".join(parts)


@dataclass
class _Resolution:
    problems: list[str]
    id_to_ordinal: dict[str, int]
    move_targets: dict[str, Path]
    unknown_id: int
    unknown_target: int


def _resolve(
    envelope: BatchEnvelope,
    source_body: str,
    *,
    index: EntityIndex,
    source_path: Path,
) -> _Resolution:
    """Validate every record against ONE snapshot of *source_body* (AC5).

    Returns a :class:`_Resolution` whose ``problems`` is non-empty exactly
    when the whole batch must be refused; callers must not write anything
    in that case.

    *source_path* is required so a ``move`` target that resolves to the
    SAME physical page as the source is refused unconditionally — never
    added to ``move_targets`` — regardless of how the two uids got there.
    :func:`load_batch` already rejects the ordinary case (``target_uid ==
    source_uid``) at parse time; this is the belt-and-suspenders check for
    the one other way it could happen: two distinct uid strings in the
    wiki resolving, via :class:`~athenaeum.models.EntityIndex`, to the same
    path (a duplicate ``uid:`` in the corpus — a pre-existing data-integrity
    issue this module does not otherwise guard against, but that is no
    reason to let it reach the double-write this check exists to prevent).
    """
    raw_bullets, _definitions = parse_bullets(source_body)
    id_to_ordinal = {bullet_id(ordinal, raw): ordinal for ordinal, raw, _refs in raw_bullets}

    problems: list[str] = []
    move_targets: dict[str, Path] = {}
    unknown_id = 0
    unknown_target = 0
    for rec in envelope.records:
        if rec.bullet_id not in id_to_ordinal:
            problems.append(f"{rec.bullet_id}: unknown bullet id")
            unknown_id += 1
            continue
        if rec.action == MOVE:
            target_path = index.get_by_uid(rec.target_uid)
            if target_path is None:
                problems.append(
                    f"{rec.bullet_id}: target uid {rec.target_uid!r} names no wiki page"
                )
                unknown_target += 1
                continue
            if target_path == source_path:
                problems.append(
                    f"{rec.bullet_id}: target uid {rec.target_uid!r} resolves to the "
                    "same page as the batch's own source_uid"
                )
                unknown_target += 1
                continue
            move_targets[rec.bullet_id] = target_path
    return _Resolution(
        problems=problems,
        id_to_ordinal=id_to_ordinal,
        move_targets=move_targets,
        unknown_id=unknown_id,
        unknown_target=unknown_target,
    )


# --- dry run -----------------------------------------------------------------


def dry_run_report(
    wiki_root: Path,
    envelope: BatchEnvelope,
    *,
    index: EntityIndex | None = None,
    config: dict[str, Any] | None = None,
) -> DryRunReport:
    """``--dry-run``: counts only (AC4). Never reads or writes a target
    page's content, and never writes anything — including the ledger.
    """
    from athenaeum.config import resolve_page_flag_bytes, resolve_page_size_threshold_chars

    idx = index if index is not None else EntityIndex(wiki_root)
    source_path = idx.get_by_uid(envelope.source_uid)
    if source_path is None:
        raise NoteCorrectionError(f"{envelope.source_uid}: no wiki page carries this uid")

    threshold_chars = resolve_page_size_threshold_chars(config)
    flag_bytes = resolve_page_flag_bytes(config)
    meta, body = _read_page(source_path)
    text_before = source_path.read_text(encoding="utf-8")
    bytes_before = len(text_before.encode("utf-8"))

    if previously_applied(wiki_root, envelope.batch_id):
        return DryRunReport(
            batch_id=envelope.batch_id,
            records_total=len(envelope.records),
            moved=0,
            dropped=0,
            refused_unknown_id=0,
            refused_unknown_target=0,
            replay=True,
            body_chars_before=len(body),
            body_chars_after=len(body),
            page_size_threshold_chars=threshold_chars,
            file_bytes_before=bytes_before,
            file_bytes_after=bytes_before,
            page_flag_bytes=flag_bytes,
        )

    resolution = _resolve(envelope, body, index=idx, source_path=source_path)
    if resolution.problems:
        return DryRunReport(
            batch_id=envelope.batch_id,
            records_total=len(envelope.records),
            moved=0,
            dropped=0,
            refused_unknown_id=resolution.unknown_id,
            refused_unknown_target=resolution.unknown_target,
            replay=False,
            body_chars_before=len(body),
            body_chars_after=len(body),
            page_size_threshold_chars=threshold_chars,
            file_bytes_before=bytes_before,
            file_bytes_after=bytes_before,
            page_flag_bytes=flag_bytes,
        )

    moved = sum(1 for r in envelope.records if r.action == MOVE)
    dropped = sum(1 for r in envelope.records if r.action == DROP)
    ordinals = {resolution.id_to_ordinal[r.bullet_id] for r in envelope.records}
    new_body = _strip_bullets(body, ordinals)

    # Simulated "after" text, byte-identical to what apply_batch would
    # actually write (same render formula _bump_and_render uses) — but
    # without calling validate_wiki_meta, so a dry run reports counts even
    # against a batch whose RESULT would fail frontmatter validation; that
    # refusal surfaces only when the operator actually applies it.
    from athenaeum.models import render_frontmatter

    today = date.today().isoformat()
    meta_after = dict(meta)
    meta_after["updated"] = today
    text_after = render_frontmatter(meta_after) + "\n" + new_body

    return DryRunReport(
        batch_id=envelope.batch_id,
        records_total=len(envelope.records),
        moved=moved,
        dropped=dropped,
        refused_unknown_id=0,
        refused_unknown_target=0,
        replay=False,
        body_chars_before=len(body),
        body_chars_after=len(new_body),
        page_size_threshold_chars=threshold_chars,
        file_bytes_before=bytes_before,
        file_bytes_after=len(text_after.encode("utf-8")),
        page_flag_bytes=flag_bytes,
    )


# --- apply -------------------------------------------------------------------


def apply_batch(
    wiki_root: Path,
    envelope: BatchEnvelope,
    *,
    index: EntityIndex | None = None,
    today: str | None = None,
) -> CorrectionOutcome:
    """Apply *envelope*: every id resolves against one snapshot, every
    check passes, before any write (AC1/AC5). Raises
    :class:`NoteCorrectionError` on refusal — nothing is written, including
    the ledger, when it raises.
    """
    idx = index if index is not None else EntityIndex(wiki_root)
    stamp = today if today is not None else date.today().isoformat()

    if previously_applied(wiki_root, envelope.batch_id):
        source_path = idx.get_by_uid(envelope.source_uid)
        body_chars = 0
        if source_path is not None:
            _, body = _read_page(source_path)
            body_chars = len(body)
        return CorrectionOutcome(
            batch_id=envelope.batch_id,
            records_total=len(envelope.records),
            results=[
                RecordResult(bullet_id=r.bullet_id, disposition="noop") for r in envelope.records
            ],
            body_chars_before=body_chars,
            body_chars_after=body_chars,
            replay=True,
        )

    source_path = idx.get_by_uid(envelope.source_uid)
    if source_path is None:
        raise NoteCorrectionError(f"{envelope.source_uid}: no wiki page carries this uid")
    meta, body = _read_page(source_path)
    definitions = parse_definitions(body)

    resolution = _resolve(envelope, body, index=idx, source_path=source_path)
    if resolution.problems:
        raise NoteCorrectionError("; ".join(resolution.problems))

    raw_bullets, _ = parse_bullets(body)
    raw_by_ordinal = {ordinal: raw for ordinal, raw, _refs in raw_bullets}

    ordinals_to_remove = {resolution.id_to_ordinal[r.bullet_id] for r in envelope.records}
    new_source_body = _strip_bullets(body, ordinals_to_remove)

    # Pre-pass: trial-render EVERY page this apply would touch before any
    # write happens, so a pydantic failure mid-run cannot leave a partially
    # applied batch — same shape as apply_report's own pre-pass in
    # page_decompose.py.
    target_cache: dict[Path, tuple[dict[str, object], str]] = {}
    for target_path in set(resolution.move_targets.values()):
        target_cache[target_path] = _read_page(target_path)

    invalid: list[str] = []
    for target_path, (t_meta, t_body) in target_cache.items():
        try:
            _bump_and_render(dict(t_meta), t_body, stamp)
        except ValidationError as exc:
            invalid.append(f"{target_path.name} ({_error_shape(exc)})")
    try:
        _bump_and_render(dict(meta), new_source_body, stamp)
    except ValidationError as exc:
        invalid.append(f"{source_path.name} ({_error_shape(exc)})")
    if invalid:
        raise NoteCorrectionError(
            f"{len(invalid)} page(s) would fail frontmatter validation: " + "; ".join(invalid)
        )

    # Write phase (in memory first — nothing touches disk until the commit
    # loop below). Records are applied in batch order; a target touched by
    # two records in the same batch composes both in memory, so the
    # second record's _next_numeric_label call sees the label the first
    # one just added.
    results: list[RecordResult] = []
    for rec in envelope.records:
        if rec.action == DROP:
            results.append(RecordResult(bullet_id=rec.bullet_id, disposition="dropped"))
            continue
        ordinal = resolution.id_to_ordinal[rec.bullet_id]
        raw = raw_by_ordinal[ordinal]
        target_path = resolution.move_targets[rec.bullet_id]
        t_meta, t_body = target_cache[target_path]

        carried = _carry_footnote(raw, definitions)
        if carried is not None:
            _old_label, text = carried
            new_label = _next_numeric_label(t_body)
            bullet_line = _renumber(raw, new_label)
            definition_line = f"[^{new_label}]: {text}"
        else:
            new_label = _next_numeric_label(t_body)
            transport = (
                f"moved from {envelope.source_uid} by note-correction "
                f"{envelope.batch_id}/{rec.bullet_id} on {stamp}"
            )
            bullet_line = f"{raw}[^{new_label}]"
            definition_line = f"[^{new_label}]: {transport}"

        t_body = _insert_fact(t_body, bullet_line, definition_line)
        target_cache[target_path] = (t_meta, t_body)
        results.append(RecordResult(bullet_id=rec.bullet_id, disposition="moved"))

    # Commit. Every touched page gets exactly one write. This relies on
    # target_cache never containing source_path: a move onto the source
    # page is refused at load_batch time (target_uid == source_uid) and
    # again, belt-and-suspenders, in _resolve (target_path == source_path)
    # for the one other way they could collide — see both docstrings. If
    # source_path were a key here, this loop would write it with the
    # appended bullet and the unconditional write just below would
    # immediately clobber that with new_source_body, silently destroying
    # the moved line; the two refusals above are what make this loop safe.
    for target_path, (t_meta, t_body) in target_cache.items():
        atomic_write_text(target_path, _bump_and_render(dict(t_meta), t_body, stamp))
    atomic_write_text(source_path, _bump_and_render(dict(meta), new_source_body, stamp))

    outcome = CorrectionOutcome(
        batch_id=envelope.batch_id,
        records_total=len(envelope.records),
        results=results,
        body_chars_before=len(body),
        body_chars_after=len(new_source_body),
        replay=False,
    )
    append_note_corrections_ledger(wiki_root, outcome)
    return outcome
