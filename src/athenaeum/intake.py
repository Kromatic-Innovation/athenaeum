# SPDX-License-Identifier: Apache-2.0
"""Raw-intake discovery + tier-0 passthrough primitives (issue athenaeum#545).

These three functions — :func:`discover_raw_files`, :func:`discover_auto_memory_files`,
and :func:`tier0_passthrough` — were previously module-level in
:mod:`athenaeum.librarian` (the L4 run-loop hub). Because they are shared
*inputs* to the run loop rather than part of it, the SCC siblings that need
them (``merge``, ``tiers``, ``batch``, ``status``, ``drain``) had to reach
BACK into ``librarian`` via function-local (deferred) imports — the
load-bearing back-edges that formed the librarian-centered import cycle.

Hoisting them DOWN to this leaf module lets every SCC sibling import them at
TOP level from here, so those deferred back-edges become unnecessary and are
removed (see issue athenaeum#545). :mod:`athenaeum.vecmath` (issue athenaeum#542) is the
precedent for this "hoist a shared primitive to a lower layer to dissolve a
cycle" move.

Layering: L2 primitive. Imports only leaf/service modules that do NOT import
any SCC member back — :mod:`athenaeum.models`, :mod:`athenaeum.config`,
:mod:`athenaeum.ephemeral`, :mod:`athenaeum._lint`, :mod:`athenaeum.schemas`,
:mod:`athenaeum.atomic_io`, and (issue athenaeum#797) :mod:`athenaeum.corrections`, a
peer L2 primitive. It must NEVER import ``librarian``, ``merge``,
``tiers``, ``pending_merges``, ``batch``, ``status``, ``retire``, or
``wiki_dedupe`` (that would re-introduce the cycle this module exists to
break). ``librarian`` re-exports these three names for backward compatibility,
so existing ``from athenaeum.librarian import discover_raw_files`` call sites
(and the public ``athenaeum.discover_raw_files`` re-export) keep working.

Issue athenaeum#797 (`docs/field-corrections.md` §3.1): a field-correction batch is
a `.jsonl` file living in the ORDINARY `raw/<source>/` tree — no reserved
subtree, no second discovery walk. :func:`discover_raw_files` recognizes one
by shape (its first line parses as a valid batch envelope, per
:func:`athenaeum.corrections.parse_batch_envelope`) and skips it — claimed by
the correction phase, which reads it directly rather than through this
function. Every OTHER `.jsonl` (malformed batch, unknown `schema_version`,
or a file that never claimed to be a batch at all) is ordinary intake,
exactly like a `.md` file — this is the only correction-shape knowledge this
function carries, and it must not carry any more than that (§3.1: "There is
no reserved subtree and no separate discovery function").
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Iterable, Sequence
from datetime import date
from pathlib import Path
from typing import Any, cast

from athenaeum._lint import _strip_self_reference
from athenaeum.atomic_io import atomic_write_text
from athenaeum.compiled_exempt import load_exempt
from athenaeum.config import (
    load_config,
    resolve_ephemeral_scopes,
    resolve_extra_intake_roots,
    resolve_non_intake_sources,
    resolve_operational_markers,
    resolve_raw_file_max_bytes,
    resolve_raw_retention_max_file_bytes,
    resolve_raw_retention_max_source_bytes,
)
from athenaeum.corrections import parse_batch_envelope
from athenaeum.dimensions import stamp_recorded_time, validate_intake_temporal
from athenaeum.ephemeral import classify_ephemeral
from athenaeum.models import (
    AutoMemoryFile,
    EntityIndex,
    RawFile,
    WikiEntity,
    coerce_source_type,
    parse_asserter,
    parse_bucket,
    parse_claim_kind,
    parse_deprecated,
    parse_frontmatter,
    parse_model,
    parse_on_behalf_of,
    parse_refines,
    parse_superseded_by,
    parse_supersedes,
    render_frontmatter,
    safe_source_ref,
    slugify,
    validity_bound_str,
)
from athenaeum.person_registry import PERSON_TYPE, PersonRegistry, PersonRegistryEntry
from athenaeum.schemas import validate_wiki_meta

log = logging.getLogger(__name__)

# Raw file naming: {timestamp}-{uuid8}.md or (issue athenaeum#797) {timestamp}-{uuid8}.jsonl
# -- the same filename convention a correction batch uses
# (docs/field-corrections.md §3.1), widened here so a batch's timestamp/uuid
# still parse like any other raw-intake file. The extension is NOT what
# decides whether a .jsonl is claimed by the correction phase -- shape
# (parse_batch_envelope on the first line) decides that, in the caller below.
RAW_FILE_RE = re.compile(r"^(\d{8}T\d{6}Z?)-([0-9a-f]{8})\.(?:md|jsonl)$", re.IGNORECASE)

# Auto-memory file naming: <prefix>_<slug>.md where prefix is one of
# feedback|project|reference|user|Recall. Slug is underscore-separated
# lowercase, but the regex only constrains the prefix — typo bodies
# (e.g. project_foo_bar.md) must still match so C2 clustering
# can dedupe them downstream. The ``Recall`` prefix is capitalized in
# production (see raw/auto-memory/.../Recall_architecture.md); lowercase
# ``recall_`` is also accepted defensively.
AUTO_MEMORY_FILE_RE = re.compile(
    r"^(feedback|project|reference|user|Recall|recall)_(.+)\.md$"
)

#: Auto-memory types accepted from a file's OWN FRONTMATTER when its filename
#: misses :data:`AUTO_MEMORY_FILE_RE`. Same closed vocabulary as that regex's
#: prefix group -- the frontmatter fallback widens WHERE the type may be
#: declared, never WHICH types are legal.
#:
#: Claude Code's memory writer names files ``<kebab-slug>.md`` and records the
#: type as ``metadata.type`` in frontmatter rather than as a filename prefix.
#: Measured on the live store 2026-08-25: 2 of 188 files carried a conforming
#: filename, so 186 real memories were silently invisible to every discovery
#: path -- the exact failure athenaeum#836's audit exists to surface, and the
#: count grew every session.
_AUTO_MEMORY_FRONTMATTER_TYPES: frozenset[str] = frozenset(
    {"feedback", "project", "reference", "user", "recall"}
)


def auto_memory_type_from_frontmatter(meta: dict[str, Any] | None) -> str | None:
    """Return the auto-memory type declared in *meta*, or ``None``.

    Reads ``metadata.type`` (the shape Claude Code's memory writer emits),
    falling back to a top-level ``memory_type``. Returns ``None`` for any
    value outside :data:`_AUTO_MEMORY_FRONTMATTER_TYPES`, so an arbitrary
    ``type:`` (``person``, ``company``, ...) never turns an entity-schema
    page into auto-memory intake.

    Shared by :func:`discover_auto_memory_files` and
    :mod:`athenaeum.intake_audit` so discovery and the audit that backstops
    it cannot drift apart on what counts as claimed.
    """
    if not isinstance(meta, dict):
        return None
    block = meta.get("metadata")
    if isinstance(block, dict):
        declared = block.get("type")
    elif isinstance(block, str):
        # Observed in the live store: a handful of files collapse the block to
        # a scalar (`metadata: feedback`) instead of `metadata: {type: ...}`.
        declared = block
    else:
        declared = None
    if declared is None:
        declared = meta.get("memory_type")
    if not isinstance(declared, str):
        return None
    value = declared.strip().lower()
    return value if value in _AUTO_MEMORY_FRONTMATTER_TYPES else None

# Filenames to skip in auto-memory scope scan. ``MEMORY.md`` is the
# per-scope curated index generated by build-per-scope-memory-index.py
# (mirrors search.py's _INTAKE_SKIP_NAMES contract). Non-.md files are
# already filtered by the glob, but ``_migration-log.jsonl`` lives at
# raw/auto-memory/ root — excluded by the directory-only iteration below.
_AUTO_MEMORY_SKIP_NAMES: frozenset[str] = frozenset({"MEMORY.md"})


def _raise_if_knowledge_root_is_actually_raw_root(
    knowledge_root: Path, config: dict[str, object] | None
) -> None:
    """Raise ``ValueError`` naming ``knowledge_root`` when *knowledge_root*
    looks like it is actually ``knowledge_root/raw`` -- the raw-intake
    root itself -- one level too deep (issue athenaeum#1134).

    Every ``recall.extra_intake_roots`` entry (default ``raw/auto-memory``)
    is resolved RELATIVE TO ``knowledge_root``. Pass the raw root
    (``knowledge_root/raw``) where ``knowledge_root`` is expected and every
    entry resolves one level too deep (``raw_root/raw/auto-memory`` instead
    of the real ``raw_root/auto-memory``), so :func:`resolve_extra_intake_roots`
    finds nothing and warns -- indistinguishable, to a caller, from "no
    extra intake configured" (this is exactly the ``raw_root``-passed-as-
    ``knowledge_root`` mistake the issue's retro traces).

    Detected by noticing an entry's LAST path segment (e.g.
    ``auto-memory``) sitting directly under *knowledge_root* while the
    full configured relative entry (``raw/auto-memory``) does not exist
    there -- the one-level shift the mistake produces. Only called on the
    already-empty-roots fallback path, and only inspects MULTI-SEGMENT
    relative entries (a single-segment entry, e.g. ``auto-memory``, or an
    absolute path has no "shift" to detect). A knowledge root that simply
    has not been populated yet (no matching directory at either depth)
    triggers nothing here and falls through to the ordinary "nothing
    configured" empty-list return -- this guard flags a *positive* sign of
    misuse, never the mere absence of one.
    """
    resolved_config = config if config is not None else load_config(knowledge_root)
    recall_cfg = resolved_config.get("recall") if isinstance(resolved_config, dict) else None
    raw_entries = recall_cfg.get("extra_intake_roots") if isinstance(recall_cfg, dict) else None
    if not isinstance(raw_entries, list):
        return
    for item in raw_entries:
        if not isinstance(item, str) or not item.strip():
            continue
        entry = Path(item)
        if entry.is_absolute() or len(entry.parts) < 2:
            continue  # nothing to shift for a single-segment or absolute entry
        shifted = knowledge_root / entry.name
        intended = knowledge_root / entry
        if shifted.is_dir() and not intended.is_dir():
            raise ValueError(
                f"knowledge_root={knowledge_root!r} looks like the raw-intake "
                f"root itself, not the knowledge root: found "
                f"'{entry.name}/' directly inside it but no '{entry}/'. Pass "
                "the knowledge root (raw_root.parent), not raw_root, as "
                "knowledge_root."
            )


def _raise_if_raw_root_is_actually_knowledge_root(
    raw_root: Path, *, param_name: str
) -> None:
    """Raise ``ValueError`` naming *param_name* when *raw_root* is not a
    plausible raw-intake root but the knowledge root one level up instead
    (issue athenaeum#1134).

    A knowledge root's own two top-level children are ``wiki/`` and
    ``raw/`` (see this module's docstring and e.g.
    ``tests/test_librarian_auto_memory.py``'s fixtures). A raw-intake
    root's own children are SOURCE directories (``sessions``,
    ``auto-memory``, ``answers``, ...) -- no production source and no test
    fixture in this repo names one ``wiki`` or ``raw``. So either name
    appearing as a direct child of *raw_root* is the adjacent-wrong-root
    mistake this guards against: the caller passed the knowledge root
    (whose ``raw/`` child is the value this function actually wants)
    instead of ``knowledge_root / "raw"`` itself.

    Only fires when *raw_root* already exists as a directory -- a
    not-yet-created or genuinely empty raw root is a legitimate,
    empty-backlog call that existing callers rely on returning ``[]``,
    never raising.
    """
    if not raw_root.is_dir():
        return
    for marker in ("wiki", "raw"):
        if (raw_root / marker).is_dir():
            raise ValueError(
                f"{param_name}={raw_root!r} looks like a knowledge_root (it "
                f"has a '{marker}/' child) -- pass the raw-intake directory "
                f"itself (knowledge_root / 'raw'), not its parent, as "
                f"{param_name}."
            )


def discover_auto_memory_files(
    knowledge_root: Path | None = None,
    config: dict[str, object] | None = None,
) -> list[AutoMemoryFile]:
    """Find all auto-memory intake files under ``raw/auto-memory/<scope>/``.

    Uses :func:`resolve_extra_intake_roots` to pick up the auto-memory
    root from config (``recall.extra_intake_roots``) — does NOT hard-code
    the path. This keeps the config surface single-sourced with the
    recall index builder.

    Returns a list of :class:`AutoMemoryFile` records sorted by
    ``(scope, filename)``. ``MEMORY.md`` files and non-directory entries
    at the auto-memory root (e.g. ``_migration-log.jsonl``) are excluded.
    The ``_unscoped/`` directory is included as a scope alongside named
    scopes — its files are first-class memories, not metadata.

    ``knowledge_root`` must be the KNOWLEDGE root (the parent of ``wiki/``
    and ``raw/``), never ``raw_root`` itself -- the two are adjacent,
    type-identical :class:`Path` values, and passing the wrong one used to
    fail open (issue athenaeum#1134): it silently returned ``[]``, reading
    exactly like a truthful "nothing here" instead of the caller error it
    is. When the ordinary resolution finds no extra intake roots, this
    function now additionally checks for that specific mistake (see
    :func:`_raise_if_knowledge_root_is_actually_raw_root`) and raises
    ``ValueError`` naming ``knowledge_root`` when it detects it, rather
    than falling through.

    ``config`` distinguishes two DIFFERENT callers (issue athenaeum#1134 AC3):

    - ``config=None`` (the default) loads ``knowledge_root``'s
      ``athenaeum.yaml`` merged with the code defaults (see
      :func:`athenaeum.config.load_config`) -- the default
      ``recall.extra_intake_roots`` is ``["raw/auto-memory"]``, so a caller
      who passes nothing gets the normal, populated behavior.
    - ``config={}`` (or any dict with no ``recall`` key) is taken
      LITERALLY, exactly like every other ``resolve_*`` helper in
      :mod:`athenaeum.config` treats an explicit config dict -- it is
      NEVER merged with disk/defaults. An empty (or ``recall``-less) dict
      therefore means "this config explicitly configures zero extra
      intake roots," and this function returns ``[]``, the same as a
      correctly-identified root that genuinely has nothing configured
      (AC2's bypass). This is a real behavioral difference from
      ``config=None``, not a bug -- see
      ``TestConfigNoneVersusEmptyDict`` in
      ``tests/test_intake_root_guard.py`` for both directions pinned.
    """
    if knowledge_root is None:
        knowledge_root = Path.home() / "knowledge"

    # resolve_extra_intake_roots returns absolute paths for every
    # configured intake root; in the default config the only entry is
    # raw/auto-memory but callers can configure more, so we iterate all.
    roots = resolve_extra_intake_roots(knowledge_root, config=config)
    if not roots:
        _raise_if_knowledge_root_is_actually_raw_root(knowledge_root, config)
        return []

    # Issue athenaeum#278: resolve the ephemeral/operational classifier inputs once.
    # An ephemeral-scope OR ``ephemeral: true``-flagged intake is dropped
    # HERE -- the cleanest choke point -- so it is never clustered or
    # materialized into a durable ``wiki/auto-*.md`` page. Drops are logged
    # with their reason; the raw file stays on disk (the move-then-retire
    # pass only touches members that landed in a wiki entry), so a dropped
    # file is simply re-evaluated (and re-dropped) idempotently next run.
    resolved_config = config if config is not None else load_config(knowledge_root)
    ephemeral_scopes = resolve_ephemeral_scopes(resolved_config)
    operational_markers = resolve_operational_markers(resolved_config)
    dropped_ephemeral = 0

    files: list[AutoMemoryFile] = []
    for root in roots:
        if not root.is_dir():
            continue
        # Directory-only iteration at the root level. This is how we
        # skip _migration-log.jsonl and any other non-scope sibling
        # files without relying on the .md glob alone.
        for scope_dir in sorted(root.iterdir()):
            if not scope_dir.is_dir():
                continue
            scope = scope_dir.name
            for fpath in sorted(scope_dir.glob("*.md")):
                if fpath.name in _AUTO_MEMORY_SKIP_NAMES:
                    continue
                m = AUTO_MEMORY_FILE_RE.match(fpath.name)
                if m is None and RAW_FILE_RE.match(fpath.name):
                    # Entity-schema file (<timestamp>-<uuid8>.md) parked in a
                    # scope dir -- a deliberate silent fall-through, claimed
                    # by the entity tier's own discovery, not auto-memory's.
                    continue
                try:
                    text = fpath.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    continue
                meta, _body = parse_frontmatter(text)
                if m is not None:
                    memory_type = m.group(1).lower()
                else:
                    # Filename misses the convention -- fall back to the type
                    # the file declares about ITSELF. A file that declares no
                    # recognised type is still skipped, so this widens the
                    # claim predicate without loosening it.
                    declared = auto_memory_type_from_frontmatter(meta)
                    if declared is None:
                        continue
                    memory_type = declared
                # Issue athenaeum#278: drop ephemeral/operational intake before it can
                # be clustered + merged into a permanent wiki entity.
                drop_reason = classify_ephemeral(
                    scope,
                    meta,
                    _body,
                    ephemeral_scopes=ephemeral_scopes,
                    operational_markers=operational_markers,
                )
                if drop_reason is not None:
                    dropped_ephemeral += 1
                    log.info(
                        "auto-memory: dropping ephemeral intake %s - %s",
                        fpath,
                        drop_reason,
                    )
                    continue
                name = str(meta.get("name", "")) if meta else ""
                description = str(meta.get("description", "")) if meta else ""
                origin_session_id = meta.get("originSessionId") if meta else None
                if origin_session_id is not None:
                    origin_session_id = str(origin_session_id)
                origin_turn_raw = meta.get("originTurn") if meta else None
                origin_turn: int | None
                try:
                    # origin_turn_raw is an arbitrary YAML scalar (object);
                    # int() rejects that statically even though at runtime
                    # it accepts str/float/bool/etc. Any type mismatch not
                    # covered by int()'s overloads still raises TypeError,
                    # caught below exactly as before.
                    origin_turn = (
                        int(cast(str, origin_turn_raw))
                        if origin_turn_raw is not None
                        else None
                    )
                except (TypeError, ValueError):
                    origin_turn = None
                sources_raw = meta.get("sources") if meta else None
                if isinstance(sources_raw, list):
                    sources = [str(s) for s in sources_raw]
                else:
                    sources = []
                # Issue athenaeum#260 (slice A of athenaeum#259): origin-traced provenance.
                # Missing source_type defaults to ``inferred``; source_ref is
                # the ultimate reference and is never this file's own name.
                source_type = coerce_source_type(
                    meta.get("source_type") if meta else None
                )
                # Guard the explicit path: a frontmatter source_ref that is a
                # raw filename (or any ``.md``) is rejected to "" rather than
                # cited as the ultimate source (athenaeum#260 invariant).
                source_ref = safe_source_ref(
                    meta.get("source_ref") if meta else None, ""
                )
                # Lane 1 / athenaeum#167: declared refines/supersedes relationships.
                # Malformed entries raise — surfacing the bad file rather
                # than silently dropping the declaration.
                try:
                    refines = parse_refines(meta if meta else None)
                    supersedes = parse_supersedes(meta if meta else None)
                except ValueError as exc:
                    log.warning(
                        "auto-memory %s: invalid refines/supersedes (%s); "
                        "treating as empty",
                        fpath,
                        exc,
                    )
                    refines = []
                    supersedes = []
                # Issue athenaeum#173 / athenaeum#181: drop refines/supersedes self-references.
                refines, supersedes = _strip_self_reference(
                    name, refines, supersedes, fpath
                )
                # Issue athenaeum#191: non-destructive inactive markers.
                meta_for_markers = meta if meta else None
                files.append(
                    AutoMemoryFile(
                        path=fpath,
                        origin_scope=scope,
                        memory_type=memory_type,
                        name=name,
                        description=description,
                        origin_session_id=origin_session_id,
                        origin_turn=origin_turn,
                        sources=sources,
                        refines=refines,
                        supersedes=supersedes,
                        superseded_by=parse_superseded_by(meta_for_markers),
                        deprecated=parse_deprecated(meta_for_markers),
                        source_type=source_type,
                        source_ref=source_ref,
                        # Issue athenaeum#326: channel-split provenance annotations.
                        model=parse_model(meta_for_markers),
                        on_behalf_of=parse_on_behalf_of(meta_for_markers),
                        asserter=parse_asserter(meta_for_markers),
                        # Issue athenaeum#327: epistemic claim kind (fail-open when
                        # absent/unrecognized → "" unclassified).
                        claim_kind=parse_claim_kind(meta_for_markers),
                        # Issue athenaeum#308: claim-level temporal validity bounds.
                        valid_from=validity_bound_str(meta_for_markers, "valid_from"),
                        valid_until=validity_bound_str(meta_for_markers, "valid_until"),
                        # Issue athenaeum#904: optional decay bucket, set at intake by
                        # ``remember()`` or a shape rule. Fail-open read — an
                        # invalid on-disk value is treated as unset, never
                        # crashes discovery (rejection happens at write time).
                        bucket=parse_bucket(meta_for_markers),
                    )
                )
    if dropped_ephemeral:
        log.info(
            "auto-memory: dropped %d ephemeral/operational intake file(s) "
            "before clustering (issue athenaeum#278)",
            dropped_ephemeral,
        )
    return files


def _is_claimed_correction_batch(fpath: Path) -> bool:
    """True when *fpath* is a `.jsonl` whose first line is a valid batch
    envelope (issue athenaeum#797, `docs/field-corrections.md` §3.1) — claimed by
    the correction phase, which reads it directly, rather than by this
    discovery function.

    Only the FIRST LINE is read (a batch may carry thousands of records;
    reading the whole file just to decide visibility defeats the streaming
    point of the format). Any read failure (permission error, bad encoding)
    or an empty first line (a zero-byte file) means "not a valid envelope" —
    conservatively falls through to ordinary intake, never silently
    disappears.
    """
    try:
        with fpath.open("r", encoding="utf-8") as fh:
            first_line = fh.readline()
    except (OSError, UnicodeDecodeError):
        return False
    if not first_line.strip():
        return False
    return parse_batch_envelope(first_line) is not None


def _discover_raw_files_in_dir(
    scan_dir: Path,
    *,
    source: str,
    exempt_refs: Iterable[str] | None,
    raw_file_max_bytes: int | None,
) -> list[RawFile]:
    """Glob `*.md`/`*.jsonl` directly inside *scan_dir* and return the
    resulting :class:`RawFile` list, tagged with *source* (the OWNING
    `raw/<source>/` directory name — never *scan_dir* itself, so a nested
    subdirectory's files still carry the same `source` a top-level file in
    the same source would, and `match.source`/non-intake exclusion continue
    to key off that one name regardless of how deep a file actually sits).

    Shared by :func:`discover_raw_files` for both the source directory
    itself and (issue athenaeum#974) each of its direct subdirectories — the
    candidate-filtering logic (`.gitkeep`, compiled-exempt, claimed
    correction batch, filename parse) must stay IDENTICAL at both depths, so
    it lives here once rather than being duplicated per call site.
    """
    files: list[RawFile] = []
    candidates = sorted({*scan_dir.glob("*.md"), *scan_dir.glob("*.jsonl")})
    for fpath in candidates:
        if fpath.name == ".gitkeep":
            continue
        # Issue athenaeum#903 (`retain`): compiled-exempt — a preserved source
        # document. Still on disk, never offered to the tiers again. The key
        # is ``RawFile.ref`` (``source/filename``), the same identifier the
        # audit ledger uses.
        if exempt_refs and f"{source}/{fpath.name}" in exempt_refs:
            continue
        if fpath.suffix.lower() == ".jsonl" and _is_claimed_correction_batch(fpath):
            # Claimed by the correction phase (§3.1) -- not ordinary
            # intake, and NOT a second discovery walk: the correction
            # phase reads this exact file directly by path/shape, it is
            # simply not appended to the list this function returns.
            continue
        m = RAW_FILE_RE.match(fpath.name)
        if m:
            files.append(
                RawFile(
                    path=fpath,
                    source=source,
                    timestamp=m.group(1),
                    uuid8=m.group(2),
                    max_content_bytes=raw_file_max_bytes,
                )
            )
        else:
            files.append(
                RawFile(
                    path=fpath,
                    source=source,
                    timestamp="",
                    uuid8="",
                    max_content_bytes=raw_file_max_bytes,
                )
            )
    return files


def discover_raw_files(
    raw_root: Path, config: dict[str, Any] | None = None
) -> list[RawFile]:
    """Find all raw intake files, sorted by timestamp.

    Issue athenaeum#843: ``config`` threads the operator's
    ``librarian.non_intake_sources`` exclusion list (mirrors how
    :func:`athenaeum.tiers.tier1_programmatic_match` already takes ``config``
    for its own operator-tuned exclusion list). A source directory named
    there is skipped WHOLE, before any glob work — the general form of the
    hardcoded ``answers`` skip below. ``None`` (the default, preserving the
    pre-athenaeum#843 call signature) excludes nothing.

    Issue athenaeum#797 (`docs/field-corrections.md` §3.1): globs `*.jsonl` in
    addition to `*.md` so a correction batch — which lives in this SAME
    ordinary `raw/<source>/` tree, no reserved subtree — is visible at all.
    A `.jsonl` is skipped (claimed by the correction phase instead of being
    appended here) ONLY when its first line is a valid batch envelope
    (:func:`athenaeum.corrections.parse_batch_envelope`); every other
    `.jsonl` — not JSON, valid JSON but not `record: "batch"`, an unknown
    `schema_version`, or a zero-byte file — is ordinary intake, appended
    below exactly like a `.md` file. This is deliberate: a malformed batch
    must still reach ordinary intake (reasoning classifies its raw text),
    never disappear.

    Issue athenaeum#898: every returned :class:`RawFile` carries
    ``max_content_bytes`` resolved from ``config`` via
    :func:`athenaeum.config.resolve_raw_file_max_bytes` (env > yaml >
    default), so a first read of ``.content`` anywhere downstream enforces
    the per-file byte bound uniformly — this is the ONE place that resolves
    it, not a per-call-site knob.

    Issue athenaeum#974: after globbing *directly* inside `raw/<source>/`,
    also glob inside each of its direct subdirectories — ONE level below the
    source directory, never deeper (the issue's own wording: "records one
    level below a source directory"). This lets a source that organises its
    own drops into subfolders (e.g. `raw/hestia/<lane>/`) still be
    discovered, without turning discovery into an unbounded recursive walk.
    A nested file's `RawFile.source` is still the TOP-LEVEL source directory
    name — not `<source>/<subdir>` — so `match.source` and
    `non_intake_sources` keep meaning exactly what they meant before this
    issue: "which `raw/<source>/` tree", not "which exact directory".

    The one exception: a source directory that is itself a configured
    ``recall.extra_intake_roots`` entry (default ``raw/auto-memory``) is
    NEVER descended into for this new subdir walk. That tree already has
    its OWN dedicated discovery function
    (:func:`discover_auto_memory_files`) and its own frontmatter schema
    (``name``/``type``/... rather than the entity schema's ``uid``/``name``)
    — every one of its real records lives one level below
    `raw/auto-memory/` (`raw/auto-memory/<scope>/<file>.md`), so this
    function's new subdir descent would otherwise start silently
    double-discovering every auto-memory file as if it were an ordinary
    entity raw file, feeding it through the WRONG schema and the WRONG
    tier ladder. Its own top-level (non-subdir) scan is untouched — it
    already finds nothing there today, since auto-memory never places a
    file directly at its root — so this guard changes nothing about
    pre-athenaeum#974 behaviour, it only stops this issue's new code from
    reaching into a tree a sibling function already owns.

    ``raw_root`` must be the raw-intake root (``knowledge_root / "raw"``),
    never ``knowledge_root`` itself -- issue athenaeum#1134: the two are
    adjacent, type-identical :class:`Path` values, and passing
    ``knowledge_root`` here used to silently mis-scan ``wiki/`` and
    ``raw/`` as if they were ordinary source directories rather than
    raising. Raises ``ValueError`` naming ``raw_root`` when *raw_root*
    exists and has a ``wiki/`` or ``raw/`` child of its own (see
    :func:`_raise_if_raw_root_is_actually_knowledge_root`) -- the shape
    only the knowledge root, never a genuine raw root, ever has.
    """
    files: list[RawFile] = []
    if not raw_root.exists():
        return files
    _raise_if_raw_root_is_actually_knowledge_root(raw_root, param_name="raw_root")

    non_intake = resolve_non_intake_sources(config)
    raw_file_max_bytes = resolve_raw_file_max_bytes(config)
    # Issue athenaeum#903: a `retain`-dispositioned file is a long-lived SOURCE
    # DOCUMENT, marked compiled-exempt in the manifest under the knowledge root.
    # Filtering here — inside discovery itself rather than at any one call site
    # — is what makes "skipped by discovery on EVERY subsequent run" true for
    # the entity loop, the drain backlog count and `status` alike, without
    # touching a single caller. Fails open to an empty set.
    exempt_refs = load_exempt(raw_root.parent)
    # Issue athenaeum#974: resolved once, same `raw_root.parent`-as-knowledge-root
    # convention `load_exempt` above already uses in this function -- the
    # trees this function's new subdir descent must never enter (see
    # docstring). Fails open to an empty set (a half-initialized knowledge
    # base with no configured extras, or none of the extras existing on
    # disk yet, excludes nothing new).
    extra_intake_roots = {
        p.resolve() for p in resolve_extra_intake_roots(raw_root.parent, config)
    }

    for source_dir in sorted(raw_root.iterdir()):
        if not source_dir.is_dir():
            continue
        source = source_dir.name
        # Issue athenaeum#843: an operator-excluded source dir holds a tool's
        # own operational artifacts, not memory content. Skip it at the source
        # level like `answers` below — before any glob/regex work, so a
        # directory of multi-megabyte logs costs one set lookup, not a walk.
        if source in non_intake:
            continue
        # Issue athenaeum#414: answer fragments under raw/answers/ are resolution
        # OUTPUT, not new intake. Re-discovering them feeds already-settled
        # rulings back through tier1-2 classification and tier4 contradiction
        # escalation, so the same ruling re-surfaces as fresh pending
        # questions on every subsequent run. Skip them at the source level,
        # before any tier classification can re-escalate them.
        if source == "answers":
            continue
        files.extend(
            _discover_raw_files_in_dir(
                source_dir,
                source=source,
                exempt_refs=exempt_refs,
                raw_file_max_bytes=raw_file_max_bytes,
            )
        )
        # Issue athenaeum#974: a source directory that is itself a dedicated
        # extra-intake-root (default: `raw/auto-memory`) already has its OWN
        # nested-subdirectory discovery function -- see the docstring's
        # "one exception" paragraph. Never descend into it here.
        if source_dir.resolve() in extra_intake_roots:
            continue
        # Issue athenaeum#974: one level below the source directory -- direct
        # subdirectories only, never a recursive walk. Each subdirectory is
        # scanned with the SAME candidate-filtering rules as the source
        # directory itself (`.gitkeep`, compiled-exempt, claimed correction
        # batch, filename parse), via the shared helper above.
        #
        # `os.scandir` rather than `Path.iterdir()` + `Path.is_dir()`: a
        # source directory ordinarily holds mostly ORDINARY FILES (the
        # existing top-level raw files), and `DirEntry.is_dir()` reads the
        # directory-type bit the OS already returned from `readdir` instead
        # of issuing a fresh `stat()` per entry -- so this loop costs
        # (in the common case) zero extra syscalls beyond the directory read
        # itself, rather than one wasted `stat()` per ordinary file just to
        # learn "not a directory".
        for entry in sorted(os.scandir(source_dir), key=lambda e: e.name):
            if not entry.is_dir():
                continue
            subdir = Path(entry.path)
            files.extend(
                _discover_raw_files_in_dir(
                    subdir,
                    source=source,
                    exempt_refs=exempt_refs,
                    raw_file_max_bytes=raw_file_max_bytes,
                )
            )
    return files


def round_robin_by_source(
    files: Sequence[RawFile],
    limit: int,
    *,
    priority_sources: Sequence[str] = (),
) -> list[RawFile]:
    """Fill a window of *limit* slots by taking from each source in turn (athenaeum#1291).

    :func:`discover_raw_files` returns its result grouped by source directory
    in ``sorted()`` order, and the run loop used to fill its ``max_files``
    window by head-truncating that list. Because the list is ordered by source
    NAME and cut from the HEAD, the window could only ever advance past a
    source once that source's own backlog dropped below the cap -- so a large,
    alphabetically-early, continuously-refilled source (``raw/auto-memory/``
    on the deployment that surfaced this) starved every lexicographically
    later source INDEFINITELY. Observed: ~2,200 records in
    ``raw/mural-board-summary/`` frozen across at least 8 consecutive runs
    while ``auto-memory`` alone exceeded the whole per-run budget.

    Round-robin bounds the worst-case wait. While ``limit`` is at least the
    number of sources, every source gets at least one slot EVERY run. Below
    that, *priority_sources* (see the turn-order bullet) rotates the head so
    each source is scheduled within ``ceil(n_sources / limit)`` runs. Either
    way the wait is bounded by the source COUNT alone -- never by any other
    source's backlog, and never by its own name's sort position. That is the
    athenaeum#1291 AC1 guarantee, and it is the smallest change to the
    existing shape (the alternatives the issue lists -- a per-source floor,
    or oldest-first across sources -- bound the wait equally but reshape more
    of the path).

    Contract:

    * **Within-source ordering is preserved exactly.** Each source's files are
      taken from the front of its own queue in discovery order, so the
      oldest-first property discovery already gives a source survives.
    * **Source turn order is first-appearance order** in *files* -- i.e. the
      ``sorted()`` source-directory order discovery produced -- EXCEPT that
      any source named in *priority_sources* takes its turn first, in the
      order given. Deterministic, so the same input always yields the same
      window.

      *priority_sources* is what makes AC1 hold when ``limit`` is smaller than
      the number of sources. Round-robin alone bounds the wait only while
      every source gets at least one slot per run; below that, a FIXED turn
      order means the same trailing sources get zero slots on every run
      forever -- starvation by sort position again, merely at a different
      threshold. The librarian passes
      :func:`athenaeum.run_summary_log.read_combined_starvation_priority`
      here — the previous run's zero-slot sources (athenaeum#1291), LONGEST-
      STARVED FIRST, THEN (athenaeum#1295) any source that received slots but
      processed zero files, longest-STALL-streak-first, appended after —
      both recovered from the athenaeum#1102 run-summary ledger so this needs
      no new state. That aging is load-bearing, not cosmetic: rotating by
      name alone lets a source keep losing its turn to sources starved only
      once and still wait unboundedly, while a rank that rises every skipped
      run reaches the head within ``ceil(n_sources / limit)`` runs. A source
      named here that has no pending files this run is simply absent from
      ``by_source`` and costs nothing.
    * **The window is interleaved, not re-concatenated.** A source's first
      file is scheduled before any source's second file, so a run that trips
      its wall-clock deadline part-way through the window has still touched
      every source rather than only the earliest ones.
    * **Budget semantics are untouched** (athenaeum#1291 AC4): this decides
      WHICH files fill the window, never how many. ``len(result) ==
      min(len(files), limit)`` always, and ``limit <= 0`` yields an empty
      window.
    """
    if limit <= 0:
        return []
    if len(files) <= limit:
        # Everything fits: no scheduling decision to make, and returning the
        # input order verbatim keeps a single-source or under-cap corpus
        # byte-identical to its pre-athenaeum#1291 behaviour.
        return list(files)
    # dict preserves insertion order, so `queues` is in first-appearance
    # (== discovery `sorted()`) source order without a second sort.
    by_source: dict[str, list[RawFile]] = {}
    for raw in files:
        by_source.setdefault(raw.source, []).append(raw)
    # Sources starved last run go first; everything else keeps discovery
    # order behind them. `dict.fromkeys` de-duplicates *priority_sources*
    # while preserving the caller's order.
    head = [s for s in dict.fromkeys(priority_sources) if s in by_source]
    order = head + [s for s in by_source if s not in set(head)]
    queues = [by_source[s] for s in order]
    cursors = [0] * len(queues)
    selected: list[RawFile] = []
    while len(selected) < limit:
        progressed = False
        for i, queue in enumerate(queues):
            if cursors[i] >= len(queue):
                continue
            selected.append(queue[cursors[i]])
            cursors[i] += 1
            progressed = True
            if len(selected) >= limit:
                break
        if not progressed:  # pragma: no cover - unreachable while limit < len(files)
            break
    return selected


def discover_shape_rule_extra_intake_files(
    raw_root: Path, config: dict[str, Any] | None = None
) -> list[RawFile]:
    """Find raw files one level below each ``recall.extra_intake_roots``
    entry (default ``raw/auto-memory``), for the SHAPE-RULE PHASE only
    (issue athenaeum#1096).

    :func:`discover_raw_files` deliberately never descends into a source
    directory that is itself a configured extra-intake root -- see its
    docstring's "one exception" paragraph. That tree already has its own
    dedicated INTAKE discovery function (:func:`discover_auto_memory_files`)
    and its own frontmatter schema, and letting ordinary intake discovery
    also walk it would double-discover every auto-memory file as if it were
    an ordinary entity raw file, through the wrong schema and the wrong
    tier ladder. That reasoning is specific to *intake*.

    `run_shape_rule_phase` (:mod:`athenaeum.rules`) is not intake -- it
    classifies candidates against operator-authored shape rules (a
    `match:` predicate on `source`/`format`/regex) and, for a `preserve`
    rule, records a disposition without compiling anything into wiki
    prose. Nothing about that phase requires or benefits from staying
    blind to a tree like `raw/auto-memory/hestia-lanes/` -- where hestia
    writes lane-log records one level below the extra-intake root
    (`writeLaneRecord`, hestia's `src/lane-record.ts`) -- so a `preserve`
    rule targeting that tree can never match today, only because its
    candidates never reach shape-rule evaluation at all.

    Mirrors :func:`discover_raw_files`'s own one-level-below subdir walk --
    same candidate-filtering logic, via the shared
    :func:`_discover_raw_files_in_dir` helper, same bound of exactly ONE
    level, never deeper -- but INVERTS its exemption: this function visits
    ONLY a source directory that IS a configured extra-intake root, and
    only that source's direct subdirectories. It does not re-walk the
    extra-intake root's own top level (:func:`discover_raw_files` already
    scans it, and finds nothing there by construction -- see that
    function's docstring), and it does not visit any non-extra-intake
    source (already covered by :func:`discover_raw_files` itself).

    A nested file's ``RawFile.source`` is still the TOP-LEVEL source
    directory name (e.g. ``auto-memory``, never ``auto-memory/<scope>``),
    matching :func:`discover_raw_files`'s own convention, so a shape rule's
    `match.source` keeps meaning "which `raw/<source>/` tree" here too.

    Intentionally a SEPARATE function, not a parameter added to
    :func:`discover_raw_files`, and not called from any intake call site --
    the only caller is :func:`athenaeum.rules.run_shape_rule_phase`, so
    ordinary intake discovery (compile, drain, status, merge, ...) is
    provably unchanged by this function's existence.

    ``raw_root`` must be the raw-intake root, never ``knowledge_root``
    itself -- same adjacent-wrong-root mistake documented on
    :func:`discover_raw_files` (issue athenaeum#1134); raises ``ValueError``
    naming ``raw_root`` under the same condition.
    """
    files: list[RawFile] = []
    if not raw_root.exists():
        return files
    _raise_if_raw_root_is_actually_knowledge_root(raw_root, param_name="raw_root")

    non_intake = resolve_non_intake_sources(config)
    raw_file_max_bytes = resolve_raw_file_max_bytes(config)
    exempt_refs = load_exempt(raw_root.parent)
    extra_intake_roots = {
        p.resolve() for p in resolve_extra_intake_roots(raw_root.parent, config)
    }

    for source_dir in sorted(raw_root.iterdir()):
        if not source_dir.is_dir():
            continue
        source = source_dir.name
        if source in non_intake:
            continue
        if source == "answers":
            continue
        if source_dir.resolve() not in extra_intake_roots:
            continue
        for entry in sorted(os.scandir(source_dir), key=lambda e: e.name):
            if not entry.is_dir():
                continue
            subdir = Path(entry.path)
            files.extend(
                _discover_raw_files_in_dir(
                    subdir,
                    source=source,
                    exempt_refs=exempt_refs,
                    raw_file_max_bytes=raw_file_max_bytes,
                )
            )
    return files


def discover_raw_backlog_bytes(
    raw_root: Path, config: dict[str, Any] | None = None
) -> int:
    """Sum on-disk byte size of the pending raw-intake backlog (issue athenaeum#909).

    Literal disk bytes, not a cost/token estimate: ``sum(path.stat().st_size
    for path in discover_raw_files(raw_root, config))``. Companion to
    :func:`discover_raw_files`'s existing file-COUNT backlog usage (``len(...)``,
    e.g. ``drain.py``/``_cmd_drain.py``/``status.py``) — this is the "M bytes"
    half of the athenaeum#909 backlog-depth trigger
    (:func:`athenaeum.config.resolve_reasoning_trigger_backlog_bytes`).

    Tolerant of a file vanishing between discovery and stat (races with a
    concurrent compile/retire pass): a per-file ``OSError`` is skipped rather
    than raising, mirroring :func:`athenaeum.models.RawFile.content`'s own
    stat-failure tolerance.

    Delegates entirely to :func:`discover_raw_files` for discovery, so the
    adjacent-wrong-root guard on ``raw_root`` (issue athenaeum#1134) applies
    here too, transitively -- no separate check needed.
    """
    total = 0
    for raw in discover_raw_files(raw_root, config):
        try:
            total += raw.path.stat().st_size
        except OSError:
            continue
    return total


def check_raw_retention(
    raw_root: Path, config: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Report raw-intake files/sources exceeding configured size ceilings
    (issue athenaeum#1269) — detect and report ONLY, never act.

    Two independent, DEFAULT-NONE dimensions, both resolved from *config*
    via :func:`athenaeum.config.resolve_raw_retention_max_file_bytes` /
    :func:`resolve_raw_retention_max_source_bytes`:

    - **Per file**: any single file anywhere under a `raw/<source>/` tree
      at or above ``max_file_bytes``.
    - **Per source**: the SUM of every file's on-disk size anywhere under
      one `raw/<source>/` tree, at or above ``max_source_bytes`` — this is
      the dimension that catches many individually-small files aggregating
      past a ceiling that no single one of them would trip (the corpus that
      motivated this issue: 943 MB across 2,247 files, ~420 KB average —
      see `athenaeum-adapters#151`).

    Deliberately walks the raw filesystem tree directly
    (:func:`os.walk`, every regular file, any extension) rather than
    delegating to :func:`discover_raw_files` — this is a storage-hygiene
    concern about literal bytes committed to a git repository, independent
    of whether a given file's extension or shape makes it something the
    entity tiers would ever offer to a rule or a reasoning pass. The mural
    corpus above is `.json`, which `discover_raw_files` never globs at all;
    a check built on top of it would silently never see the files it exists
    to report on. Also independent of `librarian.non_intake_sources` and the
    `answers` skip `discover_raw_files` applies for the same reason: those
    exclusions are about intake CLASSIFICATION, not repository size.

    Returns a summary dict, unconditionally carrying the two counters this
    issue names plus the offending paths/sources — never raises, never
    writes, never touches `discover_raw_files`'s compiled-exempt/claimed-
    correction-batch bookkeeping::

        {
            "raw-oversize-file": <int count>,
            "raw-oversize-source": <int count>,
            "oversize_files": [{"path": "<source>/<relpath>", "bytes": <int>}, ...],
            "oversize_sources": [{"source": "<source>", "bytes": <int>}, ...],
        }

    With BOTH thresholds unset (the default) this returns the all-zero
    summary immediately, without walking the filesystem at all — a fresh
    install pays no cost for a check it never armed. *raw_root* need not
    exist (mirrors :func:`discover_raw_files`'s tolerance of a
    not-yet-created raw tree): returns the all-zero summary rather than
    raising. Tolerant of a file vanishing mid-walk (race with a concurrent
    compile/retire pass), same as :func:`discover_raw_backlog_bytes`.
    """
    summary: dict[str, Any] = {
        "raw-oversize-file": 0,
        "raw-oversize-source": 0,
        "oversize_files": [],
        "oversize_sources": [],
    }
    max_file_bytes = resolve_raw_retention_max_file_bytes(config)
    max_source_bytes = resolve_raw_retention_max_source_bytes(config)
    if max_file_bytes is None and max_source_bytes is None:
        return summary
    if not raw_root.exists():
        return summary
    _raise_if_raw_root_is_actually_knowledge_root(raw_root, param_name="raw_root")

    for source_dir in sorted(raw_root.iterdir()):
        if not source_dir.is_dir():
            continue
        source = source_dir.name
        source_total = 0
        for dirpath, _dirnames, filenames in os.walk(source_dir):
            for filename in filenames:
                fpath = Path(dirpath) / filename
                try:
                    size = fpath.stat().st_size
                except OSError:
                    continue
                source_total += size
                if max_file_bytes is not None and size >= max_file_bytes:
                    summary["oversize_files"].append(
                        {
                            "path": fpath.relative_to(raw_root).as_posix(),
                            "bytes": size,
                        }
                    )
        if max_source_bytes is not None and source_total >= max_source_bytes:
            summary["oversize_sources"].append({"source": source, "bytes": source_total})

    summary["raw-oversize-file"] = len(summary["oversize_files"])
    summary["raw-oversize-source"] = len(summary["oversize_sources"])
    return summary


def tier0_passthrough(
    raw: RawFile,
    index: EntityIndex,
    wiki_root: Path,
    valid_types: list[str],
    dry_run: bool = False,
    *,
    person_registry: "PersonRegistry | None" = None,
) -> WikiEntity | None:
    """Promote a pre-structured raw-intake file to wiki/ verbatim.

    Some upstream producers (e.g. ``generate_warm_wiki.py``, contact-sync
    scripts) emit raw-intake markdown that is *already* in valid wiki
    schema — has ``uid``, ``type``, ``name``, plus rich custom-namespace
    frontmatter (``relationship:``, ``exclude:``, ``apollo_*``,
    ``current_title``, ``linkedin_url``, etc.). Sending such files through
    Tier 2/3 is wasteful (one Haiku + one Sonnet call per file) AND lossy:
    the LLM-driven path rebuilds frontmatter from a fixed allowlist and
    drops any field outside it.

    This passthrough writes the raw frontmatter + body to ``wiki/``
    byte-for-byte, only stamping ``created`` (if missing) and ``updated``
    to today. No classification runs; the index is updated so later raw
    files in the same pipeline can match against it.

    Returns the new :class:`WikiEntity` on success, or ``None`` if the
    raw is unstructured / ineligible (caller should fall through to
    Tier 1/2/3). Eligibility gate: frontmatter parses, ``uid``/``type``/
    ``name`` are non-empty, ``type`` is in the schema's allowlist, and the
    uid is not already present in the index (idempotent re-runs).

    *person_registry* (issue athenaeum#1183, keyword-only, ``None`` default):
    when supplied AND the raw declares ``type: person``, this passthrough
    targets :class:`~athenaeum.person_registry.PersonRegistry` instead of
    the general *wiki_root*/*index* — the new person page is written under
    ``person_registry.root`` (which defaults to *wiki_root* itself, see
    :func:`athenaeum.config.resolve_person_registry_root`, so an unmigrated
    corpus sees byte-identical placement) and registered into *that*
    registry rather than *index*, so it never gains a NAME-keyed entry in
    *index* (:meth:`~athenaeum.models.EntityIndex.register` also guards this
    independently — see its docstring). ``None`` (every pre-athenaeum#1183
    caller) leaves a ``type: person`` raw on the ORIGINAL *wiki_root*/*index*
    path, byte-for-byte unchanged. No LLM client is imported by this
    function or by :mod:`athenaeum.person_registry`, so this branch cannot
    make a provider call regardless of *person_registry*.
    """
    meta, body = parse_frontmatter(raw.content)
    if not meta:
        return None
    uid = str(meta.get("uid", "") or "").strip()
    etype = str(meta.get("type", "") or "").strip()
    name = str(meta.get("name", "") or "").strip()
    if not uid or not etype or not name:
        return None
    if etype not in valid_types:
        return None

    target_registry = person_registry if etype == PERSON_TYPE else None
    if target_registry is not None:
        if target_registry.get_by_uid(uid) is not None:
            return None
        target_root = target_registry.root
    else:
        if index.get_by_uid(uid) is not None:
            return None
        target_root = wiki_root

    today = date.today().isoformat()
    if not meta.get("created"):
        meta["created"] = today
    meta["updated"] = today

    filename = f"{uid}-{slugify(name)}.md"
    out_path = target_root / filename
    if out_path.exists():
        # Filename collision with a different uid would be a real bug,
        # but a same-uid existing file is already covered by the index
        # check above. Defer to Tier 1/2/3 rather than overwrite blindly.
        return None

    aliases_raw = meta.get("aliases") or []
    tags_raw = meta.get("tags") or []
    # meta is dict[str, object] (arbitrary YAML scalars). aliases/tags are
    # documented as list-shaped frontmatter fields, but the prior runtime
    # behavior tolerated any iterable (e.g. a bare string would iterate
    # per-character) and only raised TypeError for genuinely non-iterable
    # truthy values. Narrow to Iterable (not list) to keep that behavior
    # exactly; each element is coerced via str() below regardless.
    assert isinstance(aliases_raw, Iterable)
    assert isinstance(tags_raw, Iterable)
    entity = WikiEntity(
        uid=uid,
        type=etype,
        name=name,
        aliases=[str(a) for a in aliases_raw if a],
        access=str(meta.get("access", "internal")),
        tags=[str(t) for t in tags_raw if t],
        created=str(meta.get("created", today)),
        updated=str(meta.get("updated", today)),
        body=body,
    )

    # Validate frontmatter against the Pydantic schema before write. This
    # is the schema gate for the byte-for-byte passthrough — malformed
    # custom-namespace fields are still accepted (extra="allow"), but the
    # uid/type/name contract is enforced. Raises pydantic.ValidationError
    # on failure; caller treats that as a real bug, not a fall-through.
    validate_wiki_meta(meta)

    # Issue athenaeum#714 intake AC: "intake rejects observed_at later than
    # recorded_at". THIS is the intake boundary the AC names, so the anchor
    # is supplied explicitly here rather than left to the schema validator.
    # ``schemas.WikiBase``'s model validator runs on every ``validate_wiki_meta``
    # caller — including read/merge paths over pages already on disk
    # (``librarian.merge``, ``corrections``, ``batch``) — so it can only soft-flag
    # a page that carries no ``recorded_at`` of its own; rejecting there would
    # break existing data on the very paths that could repair it. A NEW page
    # entering the corpus is anchored to now, exactly as
    # ``WikiEntity.__post_init__`` would stamp it, so the hard reject lands here
    # where it belongs. ``meta`` is NOT mutated: tier 0 is a byte-for-byte
    # passthrough and must not gain a frontmatter key it was not given.
    validate_intake_temporal(
        observed_at=meta.get("observed_at"),
        recorded_at=entity.recorded_at or stamp_recorded_time(),
    )

    if dry_run:
        return entity

    atomic_write_text(
        out_path,
        render_frontmatter(meta) + "\n" + body,
    )
    if target_registry is not None:
        target_registry.register(
            PersonRegistryEntry(
                uid=uid, path=out_path, name=name, aliases=tuple(entity.aliases)
            )
        )
    else:
        index.register(entity)
    return entity


def attribute_person_observation(
    raw: RawFile,
    entry: PersonRegistryEntry,
    *,
    dry_run: bool = False,
) -> bool:
    """Attribute a raw observation to a person-registry record it was
    resolved against, LLM-free (issue athenaeum#1183 AC2).

    Companion to :func:`athenaeum.identity_resolution.resolve_person_mention`:
    once that function resolves a raw-text mention to a
    :class:`~athenaeum.person_registry.PersonRegistryEntry` — because the
    mentioned person has no :class:`~athenaeum.models.EntityIndex` entry for
    :func:`athenaeum.tiers.tier1_programmatic_match` to attribute through —
    this is the no-LLM write that records the observation on the matched
    record: *raw*'s body is prepended, as a dated bullet, immediately under
    the page's ``## Notes`` heading (most-recent-first; the heading is
    created, at the end of the body, if the page does not have one yet).
    Never a full-page LLM rewrite — this function does not import an LLM
    client/provider module at all, so no call chain through it can reach
    one.

    Returns ``True`` when the page was (or, under *dry_run*, would be)
    changed; ``False`` when *raw* carries no observation body to attribute
    (an empty/whitespace-only raw is a no-op, not an error).
    """
    _, raw_body = parse_frontmatter(raw.content)
    observation = raw_body.strip()
    if not observation:
        return False

    text = entry.path.read_text(encoding="utf-8")
    meta, body = parse_frontmatter(text)
    today = date.today().isoformat()
    bullet = f"- {today}: {observation}"

    heading = "## Notes"
    if heading in body:
        new_body = body.replace(heading, f"{heading}\n\n{bullet}", 1)
    else:
        new_body = body.rstrip("\n") + f"\n\n{heading}\n\n{bullet}\n"

    meta["updated"] = today
    validate_wiki_meta(meta)

    if dry_run:
        return True

    atomic_write_text(entry.path, render_frontmatter(meta) + "\n" + new_body)
    return True
