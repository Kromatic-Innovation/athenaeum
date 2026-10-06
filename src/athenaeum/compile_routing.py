# SPDX-License-Identifier: Apache-2.0
"""Where a prose compile's writes went, and whether they went to the right page.

Issue athenaeum#1949. A scripted submitter writes short prose files: one
correspondent and a one-paragraph conversation summary, with the correspondent
identified in frontmatter. On a live store the compile handled these
inconsistently, and one of the two observed cases is a silent drop PLUS an
unrelated write:

    The compile run that processed the file reported one update, but it was
    applied to a DIFFERENT person page -- a body rewrite with no citation of
    the file and none of the summary's content. Afterwards, no page in the
    wiki cited the file. The run's log had since rotated, so the entity
    routing for that run could not be reconstructed.

Two separable problems, and this module is both halves:

1. **Nothing checked the routing.** The correspondent resolves uniquely
   through the same ``email -> uid`` lookup the corrections path uses
   (athenaeum#858/#884), so the compile already HAS a reliable answer for
   which page a one-correspondent summary belongs on -- it simply never
   consulted it. :func:`classify_route` consults it and reports divergence.
2. **Nothing recorded the routing.** Runs logged counts (``created=2
   updated=1``), so a misroute was only reconstructible from the python log,
   which rotates. :func:`record_compile_route` writes the per-file
   ``raw ref -> written uid(s)`` join to a durable ledger instead.

**Why a separate module rather than a helper inside the librarian.** The
synchronous transport (``librarian._apply_tier3_results``) and the Batch API
transport (``batch.process_batch_run``) each have their own Tier-3 write
boundary, and ``batch.py``'s own comment states the standing contract
between them:

    The batch and synchronous transports must produce byte-identical wiki
    output -- ``TestBatchSyncEquivalence::test_wiki_output_identical`` is
    that contract -- so a write-boundary behaviour added to one belongs on
    both or on neither.

:func:`classify_route` can PREVENT a write, which is wiki-visible, so it has
to be callable from both. A guard living in either L4 module could not be.

**Why the resolution goes through :mod:`athenaeum.corrections`.** athenaeum#884
deliberately put the ``email -> contact record -> uid -> wiki page`` walk
inside the librarian rather than exposing a reverse-lookup read API, "so the
caller never needs the uid and no new caller gains contact-surface access".
This module honours that by calling the existing public
:func:`athenaeum.corrections.resolve_target` with the same target shape the
correction batch uses, rather than reaching into :mod:`athenaeum.pii` itself
or re-implementing the walk. :mod:`athenaeum.identity_resolution` has a
parallel implementation of the same walk for ``recall``-style name mentions;
reusing THAT here would add a second contact-surface-reading path next to
corrections', which is precisely what athenaeum#884's design avoids.

Fail-open throughout, like every other observability write on the compile
path (mirrors :mod:`athenaeum.adapter_provenance`): a ledger write failure is
logged and swallowed, and an unresolvable correspondent simply disables the
guard rather than failing the compile. The guard never fires for a raw file
that names no correspondent -- which is the overwhelming majority of the
corpus -- so it cannot affect ordinary intake at all.

Layering: L3 service. Imports :mod:`athenaeum.config` (cache dir
resolution), :mod:`athenaeum.corrections` (L2, target resolution),
:mod:`athenaeum.models` (``parse_frontmatter``) and :mod:`athenaeum.store`
(``append_line_durable``/``now_iso``) -- all L2 or below. Mirrors
:mod:`athenaeum.adapter_provenance`'s layering exactly. Must never import
:mod:`athenaeum.librarian`, :mod:`athenaeum.batch` or
:mod:`athenaeum.tiers` (that would close a cycle back to this module's own
callers).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any

from athenaeum.config import resolve_cache_dir
from athenaeum.models import parse_frontmatter
from athenaeum.store import append_line_durable, now_iso

if TYPE_CHECKING:  # pragma: no cover - typing only
    from athenaeum.models import EntityIndex, RawFile

log = logging.getLogger(__name__)

#: Raw-intake frontmatter key naming the correspondent a conversation-summary
#: note is about. This is the PRODUCER's key, not one invented here:
#: voltaire's shipped conversation-intake writer (``src/conversation-intake.ts``,
#: athenaeum#859's agreed submission shape) emits ``correspondent_email`` --
#: already trimmed and lowercased -- on the prose half of every triaged
#: conversation, alongside the ``.jsonl`` correction batch that targets the
#: same address as ``{"type": "person", "handle": {"email": ...}}``.
#:
#: Deliberately NOT a :data:`~athenaeum.registry.SOURCE_HANDLE_KEYS` member
#: and never written to a wiki page: the address's only home is the excluded
#: contacts surface (athenaeum#427/#437). See
#: :data:`athenaeum.corrections.EMAIL_HANDLE_KEY` for the same argument on the
#: correction-target side.
CORRESPONDENT_EMAIL_KEY = "correspondent_email"

#: Companion display-name key from the same producer, optional on its side.
#: Used ONLY to disambiguate which of several created person pages is the
#: correspondent (issue athenaeum#1948) -- never written anywhere.
CORRESPONDENT_NAME_KEY = "correspondent_name"

#: The entity type a correspondent's address may be recorded against, and the
#: only type a routing divergence is reported for. An email address identifies
#: a PERSON; an update to a company or project page from the same file is a
#: different kind of write and is not what athenaeum#1949 observed.
PERSON_ENTITY_TYPE = "person"

#: Ledger filename, under the resolved cache dir -- outside the wiki/raw
#: corpus by construction, mirroring
#: :data:`athenaeum.adapter_provenance.PROVENANCE_LEDGER_FILENAME`.
COMPILE_ROUTE_LEDGER_FILENAME = "_compile_route_records.jsonl"

#: ``divergence`` values recorded on a ledger row and used in the escalation
#: text. ``prevented`` -- the write was refused and never reached disk;
#: ``logged`` -- the write was applied and the divergence recorded. Which one
#: applies is decided by :func:`classify_route`; see its docstring for why the
#: distinction is the correspondent's own page having been written or not.
DIVERGENCE_PREVENTED = "prevented"
DIVERGENCE_LOGGED = "logged"


@dataclass(frozen=True)
class CorrespondentRef:
    """What a raw note's own frontmatter says about its correspondent.

    All three fields empty for the overwhelming majority of raw files, which
    name no correspondent at all -- so this is never an error path, and
    :attr:`address` being empty is the signal to do nothing.
    """

    address: str = ""
    display_name: str = ""
    observed_at: str = ""


def correspondent_from_raw(raw: "RawFile | None") -> CorrespondentRef:
    """Read :data:`CORRESPONDENT_EMAIL_KEY` and friends off *raw*'s frontmatter.

    One parse for all three fields. Neither call can raise at a Tier-3 write
    boundary: ``RawFile.content`` is already cached by then (both transports
    parse it at the top of the file's own processing), and
    ``parse_frontmatter`` is itself fail-open on malformed YAML -- so a note
    whose frontmatter cannot be read yields blanks rather than an exception,
    same as every other frontmatter reader on this path.

    ``observed_at`` falls back to the raw file's own timestamp, then to today.
    The producer always writes ``observed_at`` (the RFC-3339 instant of the
    most recent contact in the cycle), so the first branch is the real one;
    the fallbacks exist so a hand-authored note still records a usable date
    rather than an empty string, which would read back as an unknown
    observation time.
    """
    if raw is None:
        return CorrespondentRef()
    meta, _ = parse_frontmatter(raw.content)
    if not isinstance(meta, dict):
        meta = {}

    def _scalar(key: str) -> str:
        value = meta.get(key)
        if isinstance(value, str):
            return value.strip()
        return str(value).strip() if value is not None else ""

    observed_at = _scalar("observed_at") or raw.timestamp or date.today().isoformat()
    return CorrespondentRef(
        address=_scalar(CORRESPONDENT_EMAIL_KEY),
        display_name=_scalar(CORRESPONDENT_NAME_KEY),
        observed_at=observed_at,
    )


def resolve_correspondent_page(
    ref: CorrespondentRef,
    *,
    index: "EntityIndex",
    knowledge_root: Path | None,
    config: dict[str, Any] | None,
    excluded_index: Any | None = None,
) -> Path | None:
    """The person page *ref*'s address resolves to, or ``None``.

    Delegates to :func:`athenaeum.corrections.resolve_target` with the SAME
    target shape the correction batch submits
    (``{"type": "person", "handle": {"email": ...}}``) — so the compile and the
    corrections path can never disagree about which page an address belongs to,
    which is the property athenaeum#1949's AC1 is actually asking for. Returns
    ``None`` for every non-unique outcome that function already distinguishes
    (zero match, several distinct uids, a record with no uid, an orphan uid,
    a cross-type target), and ``None`` disables the guard: the AC's premise is
    that the correspondent "resolves uniquely", so anything else is not a
    routing question this can answer.

    ``registry_entities`` is deliberately passed empty. The address is resolved
    through the contacts surface, never through ``registry.json`` — ``email``
    is not a ``SOURCE_HANDLE_KEYS`` member and cannot be one (see
    :data:`athenaeum.corrections.EMAIL_HANDLE_KEY`), so a registry pass would
    only add a lookup that structurally cannot match.
    """
    if not ref.address:
        return None
    from athenaeum.corrections import EMAIL_HANDLE_KEY, resolve_target

    try:
        return resolve_target(
            {"type": PERSON_ENTITY_TYPE, "handle": {EMAIL_HANDLE_KEY: ref.address}},
            index=index,
            registry_entities={},
            knowledge_root=knowledge_root,
            config=config,
            excluded_index=excluded_index,
        )
    except Exception:
        log.warning(
            "compile-routing: could not resolve correspondent address "
            "(issue athenaeum#1949); routing guard disabled for this file",
            exc_info=True,
        )
        return None


def page_uid(page_path: Path) -> str:
    """The ``uid`` on *page_path*'s frontmatter, or ``""``.

    Used only to name the correspondent's page in the ledger when no write
    this file made landed on it -- the misroute case, where the uid is exactly
    what a reconstruction needs and no ``updated_uids`` entry carries it.
    """
    try:
        meta, _ = parse_frontmatter(page_path.read_text(encoding="utf-8"))
    except OSError:
        return ""
    if not isinstance(meta, dict):
        return ""
    uid = meta.get("uid")
    return str(uid).strip() if uid is not None else ""


def resolved_correspondent_uid(
    raw: "RawFile | None",
    *,
    index: "EntityIndex",
    wiki_root: Path,
    config: dict[str, Any] | None,
    excluded_index: Any | None = None,
) -> str:
    """The uid *raw*'s named correspondent resolves to, or ``""``.

    The whole read half of the guard in one call, shared by BOTH Tier-3 write
    boundaries (``librarian._apply_tier3_results`` and
    ``batch.process_batch_run``) so the two transports cannot drift — see this
    module's docstring for why that contract is load-bearing.

    ``""`` means the guard is inactive, and there are three ways to get it,
    all of them ordinary: the file names no correspondent at all (the
    overwhelming majority of the corpus), the address resolves to no page, or
    it does not resolve UNIQUELY. athenaeum#1949's AC1 premise is a
    correspondent that "resolves uniquely through the same email -> uid lookup
    the corrections path uses", so anything else is not a routing question
    this can answer -- and guessing would reintroduce the misroute from the
    other side.

    ``wiki_root.parent`` is the knowledge root, the same derivation every
    other contacts-surface caller on the compile path already uses.
    """
    if raw is None:
        return ""
    ref = correspondent_from_raw(raw)
    if not ref.address:
        return ""
    resolved = resolve_correspondent_page(
        ref,
        index=index,
        knowledge_root=wiki_root.parent,
        config=config,
        excluded_index=excluded_index,
    )
    if resolved is None:
        return ""
    return page_uid(resolved)


@dataclass(frozen=True)
class RouteVerdict:
    """Which of a file's proposed updates diverge from its correspondent.

    ``correspondent_uid`` is empty when the guard is inactive -- the file named
    no correspondent, or the address did not resolve uniquely -- in which case
    every other field is empty too and the caller changes nothing.

    ``prevented`` and ``logged`` are disjoint sets of indices into the
    caller's ``pending_updates``; a caller must skip the writes in
    ``prevented`` and apply the rest unchanged.
    """

    correspondent_uid: str = ""
    prevented: tuple[int, ...] = ()
    logged: tuple[int, ...] = ()
    reasons: tuple[str, ...] = ()

    @property
    def active(self) -> bool:
        return bool(self.correspondent_uid)

    @property
    def divergent(self) -> tuple[int, ...]:
        return tuple(sorted({*self.prevented, *self.logged}))


def classify_route(
    *,
    correspondent_uid: str,
    pending_updates: list[tuple[Path, str]],
    updated_uids: list[str],
    created_uids: list[str] | None = None,
) -> RouteVerdict:
    """Decide which proposed updates diverge from the correspondent's page.

    *correspondent_uid* is the uid :func:`resolve_correspondent_page` resolved
    the note's address to; an empty value yields an inactive verdict and the
    caller changes nothing.

    **Prevent or log — and why which.** athenaeum#1949's AC1 allows either
    ("A write from that file to a different person page is either prevented
    or logged with its reason"), and the two are not equally safe in the same
    situation, so the verdict picks between them on the one fact that
    distinguishes the observed defect from legitimate work:

    - **The correspondent's page was also written** (an update landed on
      *correspondent_uid*). Then a write to another person page is ordinary
      multi-person compilation -- the summary legitimately mentioned someone
      else -- and refusing it would be a regression. Recorded as
      :data:`DIVERGENCE_LOGGED`, applied unchanged.
    - **The correspondent's page was NOT written at all.** Then this is the
      observed shape exactly: a one-correspondent summary whose own
      correspondent received nothing, while an unrelated person's page was
      rewritten with no citation of the file. Recorded as
      :data:`DIVERGENCE_PREVENTED` and refused, so the unrelated write never
      reaches disk.

    ``created_uids`` is accepted for completeness and is only ever consulted
    for the "was the correspondent written" question. A create mints a FRESH
    uid, so it can never equal an already-resolving correspondent's uid; the
    parameter exists so a caller does not have to reason about that, and
    passing it never changes a verdict today.

    Only PERSON-page updates are ever divergent. The target's type is read
    from the pending content's own rendered frontmatter -- the exact bytes
    that would land on disk -- so a company or project update from the same
    file is left entirely alone.
    """
    if not str(correspondent_uid).strip():
        return RouteVerdict()
    wanted = str(correspondent_uid).strip()

    reached = wanted in {str(u).strip() for u in updated_uids}
    if not reached and created_uids:
        reached = wanted in {str(u).strip() for u in created_uids}

    prevented: list[int] = []
    logged: list[int] = []
    reasons: list[str] = []
    for position, (path, content) in enumerate(pending_updates):
        uid = (
            str(updated_uids[position]).strip()
            if position < len(updated_uids)
            else ""
        )
        if uid == wanted:
            continue
        meta, _ = parse_frontmatter(content)
        page_type = ""
        if isinstance(meta, dict):
            raw_type = meta.get("type")
            page_type = str(raw_type).strip().casefold() if raw_type else ""
        if page_type != PERSON_ENTITY_TYPE:
            continue
        if reached:
            logged.append(position)
            reasons.append(
                f"{DIVERGENCE_LOGGED}: update to person page {uid or path.name!r} "
                f"from a note whose correspondent is {wanted!r}; the "
                "correspondent's own page was also written, so this is treated "
                "as ordinary multi-person compilation"
            )
        else:
            prevented.append(position)
            reasons.append(
                f"{DIVERGENCE_PREVENTED}: refused an update to person page "
                f"{uid or path.name!r} from a note whose correspondent is "
                f"{wanted!r} and whose correspondent page received no write at "
                "all -- the athenaeum#1949 misroute shape"
            )
    return RouteVerdict(
        correspondent_uid=wanted,
        prevented=tuple(prevented),
        logged=tuple(logged),
        reasons=tuple(reasons),
    )


@dataclass(frozen=True)
class CompileRouteRecord:
    """One ledger row: which uid(s) a compile wrote for one raw file."""

    raw_ref: str
    source: str
    written_uids: list[str] = field(default_factory=list)
    created_uids: list[str] = field(default_factory=list)
    updated_uids: list[str] = field(default_factory=list)
    correspondent_uid: str = ""
    prevented_uids: list[str] = field(default_factory=list)
    logged_uids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "recorded_at": now_iso(),
            "raw_ref": self.raw_ref,
            "source": self.source,
            "written_uids": list(self.written_uids),
            "created_uids": list(self.created_uids),
            "updated_uids": list(self.updated_uids),
            "correspondent_uid": self.correspondent_uid,
            "prevented_uids": list(self.prevented_uids),
            "logged_uids": list(self.logged_uids),
        }


def compile_route_ledger_path(cache_dir: Path | None = None) -> Path:
    """Resolved path of the compile-route ledger.

    Same cache-dir resolution every other ledger in this codebase uses
    (:func:`athenaeum.config.resolve_cache_dir`: ``arg > env > default``).
    """
    return resolve_cache_dir(cache_dir) / COMPILE_ROUTE_LEDGER_FILENAME


def record_compile_route(
    record: CompileRouteRecord, *, cache_dir: Path | None = None
) -> None:
    """Append *record* to the durable compile-route ledger.

    This is athenaeum#1949's AC2. Runs log counts (``created=2 updated=1``), so
    a misroute was only ever reconstructible from the python log -- and the
    observed one could not be reconstructed at all, because that log had
    rotated. A durable, source-agnostic row per raw file is what makes it
    reconstructible afterwards.

    **Deliberately NOT folded into
    :func:`athenaeum.adapter_provenance.record_adapter_provenance_for_pages`**,
    which already runs at the same call site and already has the same join in
    hand. That function records an EXTERNAL source-object id, so it is a no-op
    unless the raw file's source declares an id key in
    :data:`~athenaeum.adapter_provenance.SOURCE_OBJECT_ID_KEYS` -- one entry
    today, ``mural-board-summary``. "Which uid did this file write" needs no
    such convention and must be recorded for EVERY source, including the
    conversation-intake source athenaeum#1949 is about, so widening that
    function would have meant inventing a fake external id for sources that
    have none.

    Written for every raw file that reaches a Tier-3 write boundary, including
    one that wrote nothing: an empty ``written_uids`` is exactly the observed
    symptom ("afterwards, no page in the wiki cites the file") and is the row a
    reconstruction most needs.

    Fail-open (mirrors
    :func:`athenaeum.adapter_provenance.write_adapter_provenance`): a write
    failure is logged and swallowed -- this is an audit trail, not a gate, and
    it must never break the compile it observes.
    """
    path = compile_route_ledger_path(cache_dir)
    try:
        line = json.dumps(record.to_dict(), separators=(",", ":")) + "\n"
        append_line_durable(path, line.encode("utf-8"))
    except OSError:
        log.warning(
            "compile-routing: failed to append the route record for %s to %s "
            "(issue athenaeum#1949)",
            record.raw_ref,
            path,
            exc_info=True,
        )


def read_compile_routes(cache_dir: Path | None = None) -> list[dict[str, Any]]:
    """Every ledger row, oldest first. ``[]`` when the ledger does not exist.

    The read side a reconstruction uses, and what the regression tests assert
    against. Malformed lines are skipped rather than raising -- a partially
    written row must not make the whole audit trail unreadable.
    """
    path = compile_route_ledger_path(cache_dir)
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            parsed = json.loads(line)
        except ValueError:
            log.warning("compile-routing: skipping a malformed ledger row in %s", path)
            continue
        if isinstance(parsed, dict):
            rows.append(parsed)
    return rows
