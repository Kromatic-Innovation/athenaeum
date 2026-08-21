# SPDX-License-Identifier: Apache-2.0
"""Never-ingest class list: write-refusal classes enforced at intake (issue athenaeum#968).

Part 2 of athenaeum#968 (memory-model v6's reshaped athenaeum#430): the write-refusal classes
memory-model §6.2.4 calls the "refused tier", as CONFIGURATION the intake
path consults, extending :mod:`athenaeum.authority`'s existing manifest
mechanism (:data:`athenaeum.authority.NEVER_INGEST_CLASS_SLUGS`) rather than
inventing a second config surface.

**The one-ladder rule** (from the issue's own acceptance criteria): raw
intake that is UNRECOGNISED (wrong shape/naming) already escalates to a human
via :mod:`athenaeum.intake_audit`'s pending-question queue. Raw intake that
IS recognised but matches a DECLARED refusal class is a different outcome —
no human escalation is needed (the class was already declared), but the
refusal must never be a silent drop either. This module supplies that other
rung: every refusal is appended to a durable, ids-only JSONL ledger
(:data:`REFUSALS_FILENAME`) — the "intake disposition" the issue's AC names.

Two classes ship (both are the never-ingest list's seed evidence — see the
issue's filing comment, a 2026-08-07 operator evidence log of three
witnessed live wiki pages):

- ``mirror-of-live-source`` — the claim names a value whose system of record
  is a repo/config/doc already declared in the authority manifest's
  ``sources:``. Detected by reusing :func:`athenaeum.authority.
  find_duplicate_source` UNCHANGED — the exact same deterministic
  topic/tag/name lookup that module already uses for post-hoc wiki-page
  linting, now consulted at a NEW, earlier choke point (intake) instead of
  only lint/convert. No second matcher.
- ``pending-state-todo`` — the claim asserts the CURRENT presence/absence of
  something in an external artifact ("X needs updating", "has Y landed
  yet"). Detected by a small closed phrase list
  (:data:`_PENDING_STATE_PHRASES`), the same shape
  :mod:`athenaeum.ephemeral` uses for its own multi-signal marker match — a
  phrase list, never an LLM call (this module makes zero LLM calls, matching
  every other deterministic intake-time gate in this codebase).

Neither class is enabled until an operator's authority-manifest.yaml
declares it under ``never_ingest_classes:`` — an absent/empty list is a
complete no-op (see :func:`filter_never_ingest`'s first line), so this
mechanism ships dark by default (issue athenaeum#968 DoD: "no half-wired state at
merge").

**Enforcement points** (both intake tiers) sit at each tier's own COMPILE
choke point, never inside a discovery function:

- **Auto-memory** — :func:`filter_never_ingest` is called from
  :mod:`athenaeum.librarian`'s ``_run_auto_memory_phase``, immediately after
  :func:`athenaeum.intake.discover_auto_memory_files` and before
  clustering — the auto-memory raw-intake pipeline
  (``raw/auto-memory/<scope>/...``) is the choke point closest to the
  evidence log's examples (three memory-model pages, all originally written
  via this pipeline).
- **Entity tier** — :mod:`athenaeum.librarian`'s ``process_one`` calls
  :func:`check_and_refuse` directly, right after each raw file's
  frontmatter is parsed and before Tier 0 passthrough — a manifest is
  loaded once per run and threaded into ``process_one`` exactly like that
  function's existing ``excluded_index`` parameter.

Both choke points sit AFTER discovery, never inside it, for two reasons:
:func:`athenaeum.intake.discover_auto_memory_files` and
:func:`athenaeum.intake.discover_raw_files` are documented as pure L2
discovery primitives (this module does its own independent re-read of each
candidate file's frontmatter instead, mirroring
:mod:`athenaeum.intake_audit`'s own "independent walk, discovery functions
stay pure" shape); and, for the entity tier specifically,
:func:`athenaeum.intake.discover_raw_files`'s return value is read
DIRECTLY by ``backlog_price_sheet.py`` / ``ordinary_night_table.py`` (issue
athenaeum#713's backlog-count instruments, held pending an operator decision) —
filtering inside discovery would silently move those numbers, so
``discover_raw_files`` stays byte-for-byte UNTOUCHED and a refused
entity-tier file is still discovered and still counted in the backlog; it
is simply not compiled into a wiki page this run (see
``tests/test_never_ingest.py::TestDiscoverRawFilesUnaffectedByNeverIngest``
for the regression guard on this).

**No deletion, ever** (issue athenaeum#968 AC4, a hard constraint): a refused
file is simply not offered to compilation this run — excluded from
:func:`filter_never_ingest`'s returned "kept" list (auto-memory) or
short-circuited out of ``process_one`` into ``result.skipped`` (entity
tier) — but it is NEVER removed from disk, either way. It is re-evaluated
(and, if the class still matches, re-excluded) idempotently on every
subsequent run, exactly like :func:`athenaeum.ephemeral.classify_ephemeral`'s
own drop already works. This module contains no
``unlink``/``remove``/``rmtree`` call anywhere — asserted by
``tests/test_never_ingest.py``'s static scan.

Layering: L2 primitive, alongside :mod:`athenaeum.ephemeral`. Imports
:mod:`athenaeum.authority` (L1) and :mod:`athenaeum.config` (leaf) only. Must
NEVER import :mod:`athenaeum.librarian`/:mod:`athenaeum.intake` back (the
same cycle-avoidance rule :mod:`athenaeum.intake`'s own docstring states) —
callers in ``librarian.py`` import THIS module, not the reverse.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from athenaeum.authority import (
    CLASS_MIRROR_OF_LIVE_SOURCE,
    CLASS_PENDING_STATE_TODO,
    AuthorityManifest,
    find_duplicate_source,
)
from athenaeum.config import resolve_cache_dir
from athenaeum.models import parse_frontmatter
from athenaeum.store import append_line_durable

log = logging.getLogger(__name__)

#: Filename under the cache dir for the never-ingest refusal ledger. Never
#: under the wiki/raw corpus — same discipline as
#: :data:`athenaeum.push_metrics.PUSH_RECORDS_FILENAME` — so a refusal
#: record can never itself become a claim or enter the embedded index.
REFUSALS_FILENAME = "_never_ingest_refusals.jsonl"

#: Which intake pipeline produced a refusal (issue athenaeum#968). Both tiers
#: are enforced at their own COMPILE choke point, never at discovery:
#: ``auto-memory`` in ``librarian._run_auto_memory_phase`` (right after
#: ``discover_auto_memory_files``, via :func:`filter_never_ingest`);
#: ``entity`` in ``librarian.process_one`` (per raw file, right after its
#: frontmatter is parsed, via :func:`check_and_refuse` directly) —
#: deliberately NOT inside :func:`athenaeum.intake.discover_raw_files`
#: itself, whose return value ``backlog_price_sheet.py`` /
#: ``ordinary_night_table.py`` (issue athenaeum#713, held pending an operator
#: decision) read directly for their own backlog counts and must see
#: unchanged.
NEVER_INGEST_TIER_AUTO_MEMORY = "auto-memory"
NEVER_INGEST_TIER_ENTITY = "entity"

#: Closed, fixed phrase list for the ``pending-state-todo`` class. Every
#: phrase is lower-case; matching is a case-insensitive substring test over
#: the intake's ``name``/``description``/body text (mirrors
#: :func:`athenaeum.ephemeral._marker_hits`'s own lower-cased substring
#: contract). Deliberately multi-word (unlike a single operational marker) so
#: one incidental word never false-positives — each phrase alone is a strong
#: enough signal to require no second corroborating signal, unlike
#: :mod:`athenaeum.ephemeral`'s >=2-marker rule for single-word markers.
_PENDING_STATE_PHRASES: tuple[str, ...] = (
    "has it been added",
    "has this been added",
    "has it landed",
    "has this landed",
    "needs to be added",
    "needs updating",
    "still needs",
    "not yet added",
    "not yet landed",
    "not yet updated",
    "todo:",
)

#: Frontmatter-flag truthy spellings honored for ``pending_state:`` — same
#: vocabulary as :mod:`athenaeum.ephemeral`'s ``ephemeral:`` flag.
_TRUTHY: frozenset[str] = frozenset({"true", "1", "yes", "on"})


@dataclass(frozen=True)
class NeverIngestMatch:
    """One never-ingest class match. ``detail`` is always a closed-vocabulary
    token or a manifest slug — NEVER free body/content text, matching the
    ids-only redaction discipline :mod:`athenaeum.push_metrics` already
    applies.
    """

    class_slug: str
    detail: str


def _flag_is_pending_state(meta: dict[str, Any] | None) -> bool:
    if not isinstance(meta, dict):
        return False
    flag = meta.get("pending_state")
    if flag is True:
        return True
    if isinstance(flag, str) and flag.strip().lower() in _TRUTHY:
        return True
    return False


def _pending_state_phrase_hit(meta: dict[str, Any] | None, body: str) -> str | None:
    """Return the first matched phrase from :data:`_PENDING_STATE_PHRASES`, else ``None``."""
    parts: list[str] = []
    if isinstance(meta, dict):
        parts.append(str(meta.get("name", "")))
        parts.append(str(meta.get("description", "")))
    parts.append(body or "")
    hay = " ".join(parts).lower()
    for phrase in _PENDING_STATE_PHRASES:
        if phrase in hay:
            return phrase
    return None


def classify_never_ingest(
    meta: dict[str, Any] | None,
    body: str,
    *,
    manifest: AuthorityManifest,
) -> NeverIngestMatch | None:
    """Classify one (meta, body) pair against the manifest's enabled classes.

    Checks ONLY classes present in ``manifest.never_ingest_classes`` — a
    class not listed there never matches, regardless of content (issue
    athenaeum#968's "dark by default" contract). Returns the FIRST matching class
    (``mirror-of-live-source`` checked before ``pending-state-todo``), or
    ``None`` when nothing matches or no classes are enabled.
    """
    enabled = manifest.never_ingest_classes
    if not enabled:
        return None

    if CLASS_MIRROR_OF_LIVE_SOURCE in enabled:
        source = find_duplicate_source(meta, manifest)
        if source is not None:
            return NeverIngestMatch(
                CLASS_MIRROR_OF_LIVE_SOURCE, f"topic owned by {source.slug}"
            )

    if CLASS_PENDING_STATE_TODO in enabled:
        if _flag_is_pending_state(meta):
            return NeverIngestMatch(
                CLASS_PENDING_STATE_TODO, "explicit pending_state:true flag"
            )
        hit = _pending_state_phrase_hit(meta, body)
        if hit is not None:
            return NeverIngestMatch(CLASS_PENDING_STATE_TODO, f"phrase: {hit!r}")

    return None


# ---------------------------------------------------------------------------
# Refusal ledger — durable, ids-only, append-only.
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _append_line(path: Path, line: str) -> None:
    """Append one line to *path* durably (``O_APPEND`` + fsync), via
    :func:`athenaeum.store.append_line_durable` — the single shared
    implementation issue athenaeum#980 (S5) collapsed this module's copy onto
    (design note §2.4 / §6.2)."""
    append_line_durable(path, line.encode("utf-8"))


def refusals_path(cache_dir: Path | None = None) -> Path:
    """Resolve the refusal-ledger path: ``<cache_dir>/_never_ingest_refusals.jsonl``."""
    return resolve_cache_dir(cache_dir) / REFUSALS_FILENAME


def _hash_ref(origin_scope: str, filename: str) -> str:
    """Stable, content-free digest of a refused file's identity.

    NEVER the raw filename: auto-memory filenames are
    ``<prefix>_<free-text-slug>.md`` (issue athenaeum#968 -- unlike raw
    entity-tier files, which are timestamp+hash and therefore already safe,
    see :func:`athenaeum.push_metrics.opaque_push_id`'s own docstring), so a
    free-text slug could leak content into the ledger. Hashing
    ``origin_scope/filename`` keeps the ledger row content-free while still
    letting an operator correlate a ledger row back to the file on disk by
    recomputing the same hash.
    """
    payload = f"{origin_scope}/{filename}".encode()
    return hashlib.sha256(payload).hexdigest()[:16]


@dataclass(frozen=True)
class NeverIngestRefusal:
    """One refused intake file, as it will appear in the refusal ledger.

    ``tier`` (issue athenaeum#968) is one of :data:`NEVER_INGEST_TIER_AUTO_MEMORY`
    / :data:`NEVER_INGEST_TIER_ENTITY` — which intake pipeline produced this
    refusal. Defaults to the auto-memory tier (the original, pre-entity-tier
    shape of this dataclass) so existing callers/fixtures that don't pass it
    keep working unchanged.
    """

    ts: str
    class_slug: str
    detail: str
    origin_scope: str
    file_ref_hash: str
    tier: str = NEVER_INGEST_TIER_AUTO_MEMORY

    def to_dict(self) -> dict[str, Any]:
        return {
            "ts": self.ts,
            "class": self.class_slug,
            "detail": self.detail,
            "origin_scope": self.origin_scope,
            "file_ref_hash": self.file_ref_hash,
            "tier": self.tier,
        }


def record_refusal(refusal: NeverIngestRefusal, *, cache_dir: Path | None = None) -> bool:
    """Append one refusal to the durable ledger. Best-effort, never raises.

    Mirrors :func:`athenaeum.push_metrics.record_push`'s own contract: a
    ledger-write failure must never break intake, but must never be silently
    swallowed either — it is logged at warning level.
    """
    try:
        path = refusals_path(cache_dir)
        _append_line(path, json.dumps(refusal.to_dict(), separators=(",", ":")) + "\n")
        return True
    except Exception as exc:  # noqa: BLE001 — must never break intake
        log.warning(
            "never-ingest refusal ledger write FAILED (%s): %s — this "
            "refusal will be invisible to the intake-disposition report",
            type(exc).__name__,
            exc,
        )
        return False


def read_refusals(cache_dir: Path | None = None) -> list[dict[str, Any]]:
    """Read every refusal record. Tolerates a torn trailing line; never raises."""
    path = refusals_path(cache_dir)
    if not path.is_file():
        return []
    out: list[dict[str, Any]] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


# ---------------------------------------------------------------------------
# check_and_refuse — the ONE classify+ledger primitive both tiers share.
# ---------------------------------------------------------------------------


def check_and_refuse(
    meta: dict[str, Any] | None,
    body: str,
    *,
    manifest: AuthorityManifest,
    origin_scope: str,
    filename: str,
    tier: str,
    cache_dir: Path | None = None,
    dry_run: bool = False,
) -> NeverIngestRefusal | None:
    """Classify one (meta, body) pair and, on a match, build + ledger the
    refusal in one call. Returns ``None`` when nothing matches (or no
    classes are enabled) — the common, cheap case, and the caller's signal
    to proceed with this file normally.

    The SINGLE shared primitive both intake tiers call (issue athenaeum#968):
    :func:`filter_never_ingest` (auto-memory, pre-clustering) and
    ``athenaeum.librarian.process_one`` (entity tier, per-file at the
    compile choke point — never at discovery, see :data:`NEVER_INGEST_TIER_ENTITY`'s
    docstring for why). One code path means the classify → build → log →
    ledger sequence cannot drift between the two tiers.
    """
    match = classify_never_ingest(meta, body, manifest=manifest)
    if match is None:
        return None
    refusal = NeverIngestRefusal(
        ts=_now_iso(),
        class_slug=match.class_slug,
        detail=match.detail,
        origin_scope=origin_scope,
        file_ref_hash=_hash_ref(origin_scope, filename),
        tier=tier,
    )
    log.info(
        "never-ingest: refusing %s/%s (tier=%s, class=%s, %s) -- excluded "
        "from this run's compilation, left on disk",
        origin_scope,
        filename,
        tier,
        match.class_slug,
        match.detail,
    )
    if not dry_run:
        record_refusal(refusal, cache_dir=cache_dir)
    return refusal


# ---------------------------------------------------------------------------
# Filter — the auto-memory intake choke point.
# ---------------------------------------------------------------------------


def filter_never_ingest(
    files: Iterable[Any],
    manifest: AuthorityManifest,
    *,
    cache_dir: Path | None = None,
    dry_run: bool = False,
) -> tuple[list[Any], list[NeverIngestRefusal]]:
    """Partition *files* (:class:`athenaeum.models.AutoMemoryFile` instances)
    into ``(kept, refused)`` per the manifest's ``never_ingest_classes``.

    A no-op returning ``(list(files), [])`` when the manifest declares zero
    never-ingest classes — the common case until an operator opts in (issue
    athenaeum#968's dark-by-default contract), so this costs nothing beyond one
    attribute check on every run that doesn't use the feature.

    Re-reads each candidate file's frontmatter independently (never mutates
    or relies on ``discover_auto_memory_files``'s own return shape, which
    carries only a handful of fields, not the full body) — mirrors
    :mod:`athenaeum.intake_audit`'s own "independent walk" shape. Each
    candidate is classified via the shared :func:`check_and_refuse`
    primitive (tier=:data:`NEVER_INGEST_TIER_AUTO_MEMORY`); a match is
    excluded from ``kept``, appended to ``refused``, and (unless *dry_run*)
    ledgered — it is NEVER deleted from disk (see the module docstring's
    "No deletion, ever").

    A file that cannot be re-read (permission error, vanished mid-run,
    non-UTF-8) fails OPEN — kept, never refused on an I/O error alone.
    """
    if not manifest.never_ingest_classes:
        return list(files), []

    kept: list[Any] = []
    refused: list[NeverIngestRefusal] = []
    for am in files:
        try:
            text = am.path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            kept.append(am)
            continue
        meta, body = parse_frontmatter(text)
        refusal = check_and_refuse(
            meta,
            body,
            manifest=manifest,
            origin_scope=am.origin_scope,
            filename=am.path.name,
            tier=NEVER_INGEST_TIER_AUTO_MEMORY,
            cache_dir=cache_dir,
            dry_run=dry_run,
        )
        if refusal is None:
            kept.append(am)
        else:
            refused.append(refusal)
    return kept, refused


__all__ = [
    "REFUSALS_FILENAME",
    "NEVER_INGEST_TIER_AUTO_MEMORY",
    "NEVER_INGEST_TIER_ENTITY",
    "NeverIngestMatch",
    "NeverIngestRefusal",
    "classify_never_ingest",
    "check_and_refuse",
    "refusals_path",
    "record_refusal",
    "read_refusals",
    "filter_never_ingest",
]
