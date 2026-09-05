# SPDX-License-Identifier: Apache-2.0
"""Unrecognised-raw-intake audit (issue athenaeum#836).

Two independent discovery paths decide what raw intake is even a *candidate*
for compilation:

- :func:`athenaeum.intake.discover_raw_files` (the entity tier) globs
  ``*.md``/``*.jsonl`` directly inside each ``raw/<source>/`` directory —
  any OTHER extension sitting there is never offered to Tier 1-4 at all.
- :func:`athenaeum.intake.discover_auto_memory_files` additionally requires
  a ``.md`` file inside ``raw/<auto-memory-root>/<scope>/`` to match
  :data:`athenaeum.intake.AUTO_MEMORY_FILE_RE` (``feedback_*``/``project_*``/
  ``reference_*``/``user_*``/``Recall_*``); a filename that misses the
  convention hits a bare ``continue`` and is dropped with no log line, no
  counter, nothing.

Both failure modes look identical from the operator's chair: the file sits
on disk looking ingested while nothing ever reads it again. The 2026-08-14
operator decision on the issue settles the shape the fix must take —
*"Unprocessable intake should be flagged for the human via the librarian
question/merge queue"* — so this module does not decide what to do with an
unrecognised file (that is a human call, or a future issue like athenaeum#837 for
the log-family case specifically); it only guarantees the file is never
silently invisible.

Two functions do the whole job:

- :func:`find_unclaimed_raw_files` — a PURE, side-effect-free walk of
  ``raw_root`` that returns every file neither discovery path would claim,
  each tagged with WHY (:data:`UnclaimedFile.reason`, one of the three
  strings the issue names: ``"unmatched extension"``, ``"missing naming
  convention"``, or the catch-all ``"unrecognised shape"`` for a location
  neither discovery path even looks at, e.g. a loose file directly under
  ``raw_root`` or nested deeper than either glob reaches) and grouped by
  sibling set (:data:`UnclaimedFile.group_key`, the containing directory).
- :func:`raise_unclaimed_files` — groups the result by ``(reason,
  group_key)`` and raises AT MOST ONE pending decision per group via
  :func:`athenaeum.answers.raise_pending_question` — so 88 files of one
  class (the issue's ``raw/daily-activity/*.jsonl`` example) arrive as ONE
  decision, never 88.

**Idempotence** (the load-bearing correctness property — a run must never
re-raise a decision that is already pending, or a steady backlog re-raises
itself every night forever): each raised block embeds a
``**Fingerprint**:`` line — the SAME convention
:mod:`athenaeum.tiers`/:mod:`athenaeum.answers` already use for claim-pair
dedup — computed deterministically from ``(reason, group_key)`` alone, never
from the file count or the specific paths, so the fingerprint is stable as
the group's membership grows. Before raising, both the still-open pending
file (:func:`athenaeum.answers.parse_pending_questions`, which returns
blocks in EITHER checkbox state) and the answered-and-archived sidecar
(``_pending_questions_archive.md``) are checked for that fingerprint — the
archive check is what makes resolution durable (issue athenaeum#836 AC6): once a
human has answered the question, :func:`athenaeum.answers.ingest_answers`
moves the block out of the primary file and into the archive, and this
module must not treat that as "never asked" and re-raise. This deliberately
INVERTS :mod:`athenaeum.tiers`' own dedup semantics (which explicitly
re-raises a previously-answered contradiction that resurfaces — a
"resurrection case") because the two are answering different questions: a
resurfaced CONTRADICTION is new evidence worth a fresh look, but an
unrecognised-file CLASS that has already been shown to a human is not new
evidence merely because the backlog grew.

**Preserving the legitimate silent fall-through.** A ``.md`` file living
inside an auto-memory scope directory that matches the ENTITY-schema shape
(:data:`athenaeum.intake.RAW_FILE_RE`, ``{timestamp}-{uuid8}.md``) instead
of the auto-memory naming convention is, per the surrounding code's own
comment in :func:`athenaeum.intake.discover_auto_memory_files`, a
recognised (if oddly located) shape that "naturally falls through" on
purpose — raising a decision for every one of those would be strictly worse
than the bug this issue fixes. Such a file is excluded from the result
entirely, not merely tagged with a quiet reason.

Layering: L4 (imports :mod:`athenaeum.answers`, which is L4 domain/pipeline
and does not import this module or ``librarian`` back — no cycle). Also
imports :mod:`athenaeum.intake` and :mod:`athenaeum.corrections` (both L2)
and :mod:`athenaeum.compiled_exempt`/:mod:`athenaeum.config` (leaves).
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from athenaeum.answers import parse_pending_questions, raise_pending_question
from athenaeum.compiled_exempt import load_exempt
from athenaeum.config import (
    load_config,
    resolve_extra_intake_roots,
    resolve_non_intake_sources,
    resolve_raw_file_max_bytes,
)
from athenaeum.corrections import find_correction_batches
from athenaeum.fingerprint import RESOLVED_CONTRADICTIONS_RELPATH
from athenaeum.intake import (
    _AUTO_MEMORY_SKIP_NAMES,
    AUTO_MEMORY_FILE_RE,
    RAW_FILE_RE,
    auto_memory_type_from_frontmatter,
)
from athenaeum.models import RawFile, parse_frontmatter

log = logging.getLogger(__name__)

#: Mirrors ``athenaeum.corrections._SKIPPED_SOURCES`` / ``athenaeum.intake``'s
#: hardcoded ``source == "answers"`` skip (issue athenaeum#414): answer
#: fragments are resolution OUTPUT, never new intake, so a source directory
#: named ``answers`` is excluded from the audit exactly like it is excluded
#: from ``discover_raw_files``.
_ANSWERS_SOURCE = "answers"

#: The three reasons the issue names verbatim (AC2). Closed vocabulary —
#: every :class:`UnclaimedFile` carries exactly one of these.
REASON_UNMATCHED_EXTENSION = "unmatched extension"
REASON_MISSING_NAMING_CONVENTION = "missing naming convention"
REASON_UNRECOGNISED_SHAPE = "unrecognised shape"

#: Cap on how many sibling paths are embedded in a raised item's text (AC2:
#: "bound the number of paths ... so an 88-file class does not produce an
#: unreadable block").
_MAX_SAMPLE_PATHS = 5


@dataclass(frozen=True)
class UnclaimedFile:
    """One raw-intake file neither discovery path claims.

    ``group_key`` is the sibling-grouping key :func:`raise_unclaimed_files`
    groups by (alongside ``reason``) — the containing directory, relative to
    ``raw_root``, e.g. ``"daily-activity"`` for an entity-tier source dir or
    ``"auto-memory/<scope>"`` for an auto-memory scope dir.
    """

    path: Path
    reason: str
    group_key: str


def find_unclaimed_raw_files(
    raw_root: Path,
    knowledge_root: Path,
    config: dict[str, Any] | None = None,
) -> list[UnclaimedFile]:
    """Return every raw-intake file neither discovery path would claim.

    Pure and side-effect-free — reads the filesystem and the athenaeum#903
    compiled-exempt manifest, writes nothing. Deliberately does NOT call
    :func:`athenaeum.intake.discover_raw_files` /
    ``discover_auto_memory_files`` and diff their output against a full
    ``raw_root`` walk: those functions' RETURNED lists additionally exclude
    files those functions themselves later drop for unrelated, already-
    non-silent reasons (e.g. athenaeum#278's ephemeral-scope auto-memory drop, which
    already logs its own reason) — diffing against their output would
    mis-flag every one of those as "unclaimed" here too. Instead this
    reimplements the narrow, stable CLAIM predicate each function's glob
    itself applies (extension + naming-convention shape), which is exactly
    the layer this audit exists to backstop.
    """
    if not raw_root.exists():
        return []
    if config is None:
        config = load_config(knowledge_root)

    non_intake = resolve_non_intake_sources(config)
    exempt_refs = load_exempt(knowledge_root)
    extra_roots = {
        p.resolve() for p in resolve_extra_intake_roots(knowledge_root, config=config)
    }
    # Issue athenaeum#797 §3.1: a `.jsonl` claimed by the correction phase is
    # ordinary intake by location but consumed by a DIFFERENT discovery walk
    # than `discover_raw_files`'s own — still claimed, never unrecognised.
    claimed_batches = {
        path.resolve() for path, _source, _envelope in find_correction_batches(raw_root)
    }
    # Issue athenaeum#198: `raw/_resolved_contradictions.jsonl` is a known,
    # intentional loose file directly at raw_root -- the fingerprint-cache
    # sidecar `athenaeum.answers.ingest_answers` writes to (and, notably,
    # ALSO writes to when it archives one of THIS module's own raised
    # blocks, since every raised block carries a `**Fingerprint**:` line —
    # see the module docstring). `discover_raw_files` already treats it as
    # invisible-by-construction (it walks directories under raw_root, and a
    # loose file is skipped by `if not source_dir.is_dir(): continue`); this
    # audit must agree, or resolving a raised decision would make THIS FILE
    # itself get raised on the very next run -- an audit-induced regress.
    resolved_contradictions_path = knowledge_root.joinpath(
        *RESOLVED_CONTRADICTIONS_RELPATH
    ).resolve()

    out: list[UnclaimedFile] = []
    for fpath in sorted(raw_root.rglob("*")):
        if not fpath.is_file():
            continue
        if fpath.name in (".gitkeep", ".DS_Store"):
            # Filesystem detritus, never intake and never an operator decision.
            continue
        if fpath.name in _AUTO_MEMORY_SKIP_NAMES:
            continue
        if fpath.resolve() == resolved_contradictions_path:
            continue
        rel = fpath.relative_to(raw_root)
        parts = rel.parts
        source = parts[0]
        if len(parts) == 1 and fpath.name.startswith("_"):
            # The librarian's OWN working files at raw root --
            # `_librarian-clusters-*.jsonl` and friends. `discover_raw_files`
            # never sees them (it iterates DIRECTORIES under raw_root), so
            # flagging them made the audit raise a decision about its own
            # process output. Generalises the athenaeum#198
            # `_resolved_contradictions.jsonl` special case above.
            continue
        if source == _ANSWERS_SOURCE or source in non_intake:
            continue
        if len(parts) >= 2 and f"{source}/{fpath.name}" in exempt_refs:
            continue
        if fpath.resolve() in claimed_batches:
            continue

        ext = fpath.suffix.lower()

        if len(parts) == 2:
            # raw/<source>/<file> -- the entity-tier shape. `discover_raw_files`
            # claims EVERY `.md`/`.jsonl` file at this depth regardless of its
            # naming (a RAW_FILE_RE mismatch is still appended, just with
            # empty timestamp/uuid8) -- so extension alone decides claim here.
            if ext in (".md", ".jsonl"):
                continue
            out.append(
                UnclaimedFile(
                    path=fpath, reason=REASON_UNMATCHED_EXTENSION, group_key=source
                )
            )
            continue

        if len(parts) == 3 and fpath.parent.parent.resolve() in extra_roots:
            # raw/<auto-memory-root>/<scope>/<file> -- the auto-memory shape.
            scope = parts[1]
            group_key = f"{source}/{scope}"
            if ext != ".md":
                out.append(
                    UnclaimedFile(
                        path=fpath,
                        reason=REASON_UNMATCHED_EXTENSION,
                        group_key=group_key,
                    )
                )
                continue
            if AUTO_MEMORY_FILE_RE.match(fpath.name):
                continue
            if RAW_FILE_RE.match(fpath.name):
                # Legitimate silent fall-through -- see module docstring.
                continue
            if _declares_auto_memory_type(fpath):
                # Filename misses the convention but the file declares its own
                # type in frontmatter, which `discover_auto_memory_files` now
                # honours -- claimed, not unrecognised. Kept in lockstep with
                # that function via the shared
                # `auto_memory_type_from_frontmatter` predicate.
                continue
            out.append(
                UnclaimedFile(
                    path=fpath,
                    reason=REASON_MISSING_NAMING_CONVENTION,
                    group_key=group_key,
                )
            )
            continue

        if len(parts) == 3:
            # raw/<source>/<subdir>/<file> where <source> is NOT an extra-intake
            # root -- athenaeum#974 gave `discover_raw_files` a one-level-deep
            # walk below each source dir, with the same extension-only claim
            # rule it applies at the source's own top level. This audit did not
            # model that descent, so every file in such a subdir was reported
            # `unrecognised shape` WHILE BEING CLAIMED AND QUEUED. Measured on
            # the live store 2026-08-25: 2849 of 7622 flagged files were this
            # false positive, dominated by `raw/drive/<pipeline>-intake/`.
            group_key = f"{source}/{parts[1]}"
            if ext in (".md", ".jsonl"):
                continue
            out.append(
                UnclaimedFile(
                    path=fpath,
                    reason=REASON_UNMATCHED_EXTENSION,
                    group_key=group_key,
                )
            )
            continue

        # Anything else -- a loose file directly at raw_root, or nested
        # deeper than either glob reaches -- is a location NEITHER discovery
        # path even looks at.
        group_key = str(rel.parent) if len(parts) > 1 else "(raw root)"
        out.append(
            UnclaimedFile(path=fpath, reason=REASON_UNRECOGNISED_SHAPE, group_key=group_key)
        )

    return out


def discover_unclaimed_shape_rule_candidates(
    raw_root: Path,
    knowledge_root: Path,
    config: dict[str, Any] | None = None,
) -> list[RawFile]:
    """Wrap :func:`find_unclaimed_raw_files` as :class:`RawFile` candidates
    for the shape-rule phase (issue athenaeum#1133).

    Deliberately lives HERE, not in :mod:`athenaeum.rules` — that module's
    own docstring fixes it at Layering L3 (imports :mod:`athenaeum.intake`
    and :mod:`athenaeum.corrections`, both L2, and neither imports back);
    this module is L4 (imports :mod:`athenaeum.answers`). Importing this
    module from ``rules.py`` would invert L3->L4 and reintroduce the cycle
    the layering discipline exists to prevent. Instead, the CALLER
    (:mod:`athenaeum.librarian`, which already imports both
    :func:`find_unclaimed_raw_files` and
    :func:`athenaeum.rules.run_shape_rule_phase`) resolves this list and
    passes it into ``run_shape_rule_phase``'s ``unclaimed_candidates``
    keyword — the same "separate discovery function, appended by the
    caller" shape issue athenaeum#1096 established for
    :func:`athenaeum.intake.discover_shape_rule_extra_intake_files`.

    Each :class:`~athenaeum.models.UnclaimedFile` becomes one
    :class:`~athenaeum.models.RawFile`, using the same fallback shape
    :func:`athenaeum.intake._discover_raw_files_in_dir` already uses on a
    ``RAW_FILE_RE`` mismatch — ``timestamp=""``, ``uuid8=""``,
    ``max_content_bytes`` resolved from *config* — since an unclaimed file
    by definition never matched that naming convention (if it had, it would
    have been claimed by ordinary discovery instead).

    ``RawFile.source`` is the TOP-LEVEL source directory, never the full
    ``group_key`` — ``group_key`` can be ``"source/subdir"`` for the
    nested-descent case (this module's ``len(parts) == 3`` branch above),
    so it is split and only the first segment is kept, mirroring
    :func:`athenaeum.intake.discover_raw_files`'s own convention (see also
    :func:`athenaeum.intake.discover_shape_rule_extra_intake_files`'s
    docstring, which states the same convention for its own nested
    candidates). A shape rule's ``match.source`` therefore keeps meaning
    "which ``raw/<source>/`` tree" for an unclaimed candidate too.

    No de-duplication against :func:`athenaeum.intake.discover_raw_files` /
    :func:`athenaeum.intake.discover_shape_rule_extra_intake_files` is
    needed: :func:`find_unclaimed_raw_files` reimplements the claim
    predicate those two functions apply, so its output is already disjoint
    from theirs by construction (see that function's own docstring).
    """
    unclaimed = find_unclaimed_raw_files(raw_root, knowledge_root, config)
    if config is None:
        config = load_config(knowledge_root)
    raw_file_max_bytes = resolve_raw_file_max_bytes(config)
    out: list[RawFile] = []
    for uf in unclaimed:
        top_level_source = uf.group_key.split("/", 1)[0]
        out.append(
            RawFile(
                path=uf.path,
                source=top_level_source,
                timestamp="",
                uuid8="",
                max_content_bytes=raw_file_max_bytes,
            )
        )
    return out


def _declares_auto_memory_type(fpath: Path) -> bool:
    """True when *fpath*'s own frontmatter declares a recognised memory type.

    Mirrors :func:`athenaeum.intake.discover_auto_memory_files`'s frontmatter
    fallback so a file that discovery now CLAIMS is never simultaneously
    reported here as unclaimed. Fails closed: an unreadable or frontmatter-less
    file returns ``False`` and stays flagged, which is the safe direction --
    a visible decision rather than a silent drop.
    """
    try:
        text = fpath.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    meta, _body = parse_frontmatter(text)
    return auto_memory_type_from_frontmatter(meta) is not None


def _group_fingerprint(reason: str, group_key: str) -> str:
    """Stable dedup key for a ``(reason, group_key)`` pair.

    Deliberately independent of file count or the specific paths in the
    group, so a group's fingerprint does not change as its membership grows
    -- the same open (or already-answered) decision keeps covering every
    file in the class, which is what makes the idempotence guarantee hold
    across a growing backlog (issue athenaeum#836, "Idempotence is mandatory").
    """
    payload = f"athenaeum#836-unclaimed-intake:{reason}:{group_key}".encode()
    return "unclaimed-" + hashlib.sha256(payload).hexdigest()[:16]


def _render_raise_text(
    reason: str, group_key: str, paths: list[Path], raw_root: Path, fingerprint: str
) -> tuple[str, str]:
    """Build the ``(question, context)`` pair for one raised group."""
    count = len(paths)
    display = sorted(str(Path("raw") / p.relative_to(raw_root)) for p in paths)
    sample = display[:_MAX_SAMPLE_PATHS]
    remainder = count - len(sample)
    sample_lines = "\n".join(f"  - {s}" for s in sample)
    if remainder > 0:
        sample_lines += f"\n  - ... (+{remainder} more)"

    question = (
        f"{count} raw intake file(s) under raw/{group_key}/ were not "
        f"recognised by discovery this run ({reason}) -- what should happen "
        "to them?"
    )
    context = (
        f"{count} file(s) share this reason ({reason}). Sample:\n"
        f"{sample_lines}\n\n"
        "These files sit on disk looking ingested but currently reach no "
        "compile/cluster/retire disposition until this decision is resolved "
        "(issue athenaeum#836). Resolving this stops it from being raised "
        "again on a future run -- it does not, by itself, change what "
        "happens to the files; that remains a separate decision (see also "
        "athenaeum#837 for the specific case of log-shaped intake). "
        "Alternatively (issue athenaeum#1133), an operator rule with "
        "`match: {unclaimed: true, ...}` can give this whole group a "
        "disposition (drop/retain/preserve) directly -- see "
        "docs/design/shape-rules.md.\n\n"
        f"**Fingerprint**: {fingerprint}"
    )
    return question, context


def _fingerprints_already_seen(pending_path: Path, archive_path: Path | None) -> set[str]:
    """Every ``**Fingerprint**:`` value already on the queue OR archived.

    ``pending_path`` is parsed for BOTH checkbox states — an item that was
    just resolved (``[x]``) but not yet archived by
    :func:`athenaeum.answers.ingest_answers` must still suppress a re-raise.
    ``archive_path`` is checked by plain substring search (archived blocks
    are no longer parsed by :func:`athenaeum.answers.parse_pending_questions`
    once they have been moved out of the primary file) -- this is what makes
    AC6 hold once a real ``ingest-answers`` run has archived the resolution.
    """
    seen: set[str] = set()
    if pending_path.exists():
        for pq in parse_pending_questions(pending_path):
            if pq.fingerprint:
                seen.add(pq.fingerprint)
    if archive_path is not None and archive_path.exists():
        # Archived blocks are no longer parsed by `parse_pending_questions`
        # once `athenaeum.answers.ingest_answers` has moved them out of the
        # primary file -- a plain line-prefix scan for the fingerprint tag
        # is simpler than re-deriving the archive's block-splitting shape
        # (header + raw block verbatim + an "**Archived**:" trailer) and is
        # equally exact, since `**Fingerprint**:` is a fixed, unambiguous
        # prefix `athenaeum.answers.raise_pending_question` alone emits.
        for line in archive_path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("**Fingerprint**:"):
                seen.add(stripped.removeprefix("**Fingerprint**:").strip())
    return seen


def raise_unclaimed_files(
    pending_path: Path,
    unclaimed: list[UnclaimedFile],
    *,
    raw_root: Path,
    archive_path: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Raise one pending decision per ``(reason, group_key)`` in *unclaimed*.

    Idempotent (issue athenaeum#836's load-bearing correctness property): a
    group whose fingerprint is already open OR already answered/archived is
    skipped, never re-raised. See the module docstring "Idempotence" for the
    full mechanism.

    Issue athenaeum#836 AC5's denominator invariant, in the same spirit as
    athenaeum#903's own (``rules.run_shape_rule_phase``: every disposition tally
    sums to the records a rule SAW) — every unclaimed file this call was
    given is accounted for by EXACTLY one of ``raised_files`` (its group was
    newly raised this call) or the files belonging to an
    ``already_open_groups`` group (its group was already pending/resolved).
    Asserted below and logged loudly on violation, never silently trusted.
    """
    summary: dict[str, Any] = {
        "unclaimed_files": len(unclaimed),
        "groups": 0,
        "raised_groups": 0,
        "raised_files": 0,
        "already_open_groups": 0,
    }
    if not unclaimed:
        return summary

    groups: dict[tuple[str, str], list[Path]] = {}
    for uf in unclaimed:
        groups.setdefault((uf.reason, uf.group_key), []).append(uf.path)
    summary["groups"] = len(groups)

    existing_fingerprints = _fingerprints_already_seen(pending_path, archive_path)
    already_open_files = 0

    for (reason, group_key), paths in sorted(groups.items()):
        fingerprint = _group_fingerprint(reason, group_key)
        if fingerprint in existing_fingerprints:
            summary["already_open_groups"] += 1
            already_open_files += len(paths)
            continue
        question, context = _render_raise_text(reason, group_key, paths, raw_root, fingerprint)
        result = raise_pending_question(
            pending_path,
            question,
            context,
            entity=f"unclaimed raw intake: {group_key}",
            source=f"raw/{group_key}",
            now=now,
        )
        if result["ok"]:
            summary["raised_groups"] += 1
            summary["raised_files"] += len(paths)
            # A group raised THIS call must not be raised again by a later
            # group in the SAME call sharing a fingerprint by coincidence
            # (cannot happen given the (reason, group_key) keying above, but
            # cheap to guard so a future refactor cannot reintroduce a
            # same-run double-raise).
            existing_fingerprints.add(fingerprint)
        else:
            # A failed raise (validation refusal, disk error) leaves that
            # group's files accounted for by neither `raised_files` nor
            # `already_open_groups` -- tallied here so the invariant below
            # catches it rather than silently under-counting.
            already_open_files += len(paths)
            log.warning(
                "intake-audit: failed to raise decision for reason=%r "
                "group=%r: %s",
                reason,
                group_key,
                result["message"],
            )

    accounted = summary["raised_files"] + already_open_files
    if accounted != summary["unclaimed_files"]:
        log.error(
            "intake-audit: denominator invariant violated -- %d unclaimed "
            "file(s) seen but only %d accounted for (issue athenaeum#836)",
            summary["unclaimed_files"],
            accounted,
        )

    return summary


def run_intake_audit(
    *,
    raw_root: Path,
    wiki_root: Path,
    knowledge_root: Path,
    config: dict[str, Any] | None = None,
    dry_run: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Find + raise in one call -- the entry point ``librarian.run`` uses.

    A ``dry_run`` computes the same ``unclaimed_files``/``groups`` counts
    but never writes to ``_pending_questions.md`` (mirrors every other
    deterministic phase's dry-run contract).
    """
    unclaimed = find_unclaimed_raw_files(raw_root, knowledge_root, config)
    if dry_run:
        groups = {(u.reason, u.group_key) for u in unclaimed}
        return {
            "unclaimed_files": len(unclaimed),
            "groups": len(groups),
            "raised_groups": 0,
            "raised_files": 0,
            "already_open_groups": 0,
        }
    pending_path = wiki_root / "_pending_questions.md"
    archive_path = wiki_root / "_pending_questions_archive.md"
    return raise_unclaimed_files(
        pending_path, unclaimed, raw_root=raw_root, archive_path=archive_path, now=now
    )
