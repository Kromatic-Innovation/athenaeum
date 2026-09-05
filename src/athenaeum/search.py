# SPDX-License-Identifier: Apache-2.0
"""Search backend abstraction for athenaeum — the recall/query L3 service.

**Contract:** given a query string (or an already-embedded vector), return
ranked ``(filename, page_name, score)`` hits over the wiki + configured
intake roots; given a corpus, (re)build whichever on-disk index a backend
needs to answer that query cheaply.

**Factoring rule:** this module owns QUERY and RANKING — tokenizing/
embedding a query, scoring or kNN-ranking candidates, and the incremental
index-build bookkeeping (manifests, stat pre-filter, schema versioning) that
makes ranking cheap to keep current. It does NOT own storage: it never
decides where a page lives, what its frontmatter means, or how it is
authorized — those are read from :mod:`athenaeum.models` /
:mod:`athenaeum.authority` and merely filtered on here. Fan-in is high (the
recall hook, the clusterer, the delta compiler, and the cross-scope sweep
all call into this module) — resist adding capability-specific branches;
callers adapt to the three backends' shared ``SearchBackend`` Protocol.

**Layering:** L3 service. Imports only L1 (:mod:`athenaeum.models`,
:mod:`athenaeum.store` — issue athenaeum#977, the whole-store adapter seam,
and ``KeywordBackend``'s S6 capability gate, issue athenaeum#981) and L2
(:mod:`athenaeum.authority`, :mod:`athenaeum.pii`) at module scope; never
imports L4 (the domain/pipeline modules — :mod:`athenaeum.tiers`,
:mod:`athenaeum.librarian`, etc.). ``chromadb`` (the ``vector`` backend's
engine) is an optional ``[vector]`` extra: every chromadb import in this
module is function-local so importing ``athenaeum.search`` itself never
requires chromadb to be installed — only calling into ``VectorBackend``
does.

Three backends, one ``SearchBackend`` Protocol: the default ``fts5`` backend
uses SQLite FTS5 with BM25 ranking and porter stemming; ``vector`` uses
chromadb with ``all-MiniLM-L6-v2``; ``keyword`` is a zero-setup scan-on-query
fallback. When the vector backend is configured, the example recall hook
performs a hybrid FTS5+vector merge so that short proper-noun queries still
resolve cleanly — see ``docs/design/recall-architecture.md`` for why each backend is
load-bearing.

**Invariant:** every query path enforces the SAME three exclusions before a
page can occupy a result slot — inactive/expired
(:func:`_is_recall_inactive` — issue athenaeum#904: the recall-time
sibling of :func:`athenaeum.models.is_inactive_memory` that lets an
expired ``bucket: daily`` page stay recall-visible for currency ranking
instead of being hard-filtered; see that function's docstring),
PII-flagged (:func:`athenaeum.pii.is_pii_flagged`), and audience-unauthorized
(:func:`athenaeum.models.is_page_authorized` / the per-backend audience
predicate) — pushed INSIDE the backend query (not post-filtered) so a
forbidden or excluded page can never occupy a top-k slot or push a permitted
page past the limit. A new backend MUST replicate all three or silently
regress athenaeum#191/#312/#427.

Shell hook scripts can call the module-level convenience functions
(``build_fts5_index``, ``query_fts5_index``, ``build_vector_index``,
``query_vector_index``) without constructing backend objects.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from datetime import date
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from athenaeum.authority import is_pointer_stub
from athenaeum.models import (
    AUDIENCE_PUBLIC_TOKEN,
    audience_index_string,
    audience_string_authorized,
    is_page_authorized,
    parse_deprecated,
    parse_frontmatter,
    parse_superseded_by,
    resolve_page_type,
    valid_until_expired,
    validity_bound_str,
)
from athenaeum.pii import is_pii_flagged
from athenaeum.storage import is_embedded
from athenaeum.store import FilesystemStore, ObjectMeta, Store, StoreKey

# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------

# Issue athenaeum#373: default age (days) for the periodic full-re-hash backstop that
# heals the athenaeum#370 stat pre-filter's blind spot (a content edit preserving both
# mtime and size). Referenced by every ``build_index`` signature, so it is
# defined here before the Protocol; the full rationale lives beside the manifest
# helpers below. Resolved from config by
# ``config.resolve_reindex_full_rehash_max_age_days``.
_DEFAULT_FULL_REHASH_MAX_AGE_DAYS = 7.0


@runtime_checkable
class SearchBackend(Protocol):
    """Interface that all search backends must satisfy."""

    def build_index(
        self,
        wiki_root: Path,
        cache_dir: Path,
        *,
        extra_roots: Iterable[Path] | None = None,
        incremental: bool = True,
        include_globs: Iterable[str] | None = None,
        exclude_globs: Iterable[str] | None = None,
        as_of: date | None = None,
        full_rehash_max_age_days: float = _DEFAULT_FULL_REHASH_MAX_AGE_DAYS,
        config: dict[str, Any] | None = None,
    ) -> int:
        """Build or rebuild the search index.

        Args:
            wiki_root: The primary wiki directory (shallow ``*.md`` scan,
                underscore-prefixed files excluded). Entries indexed with a
                bare filename (e.g. ``lean-startup.md``).
            cache_dir: Where the index is persisted.
            extra_roots: Additional intake roots (recursive scan). Each
                root's entries are indexed with a path of the form
                ``<root_name>/<relpath>`` so recall results disambiguate
                wiki entries from raw intake entries. Intended for the
                ``raw/auto-memory/`` intake tree, but accepts any
                directory. Files named ``MEMORY.md`` (per-scope index
                files) and non-``.md`` files are skipped.
            incremental: When ``True`` (default) and a prior manifest
                exists, diff each page's whole-file content hash against
                the stored manifest and apply only the delta — add new
                pages, re-index changed pages, delete removed pages
                (issue athenaeum#348). A no-op rebuild then touches nothing and
                returns in sub-second time regardless of corpus size.
                When ``False`` (seeding, ``reindex --full``), wipe and
                rebuild from scratch. No prior manifest also forces a full
                build. Setting ``as_of`` (below) also forces a full build.
            include_globs / exclude_globs: Optional corpus-scoping globs
                matched against the indexed name (issue athenaeum#348 COULD). The
                default (``None`` / ``None``) indexes everything — the
                Apollo contact wikis are legitimate name-recall targets and
                must stay indexed by default. This is a footprint/relevance
                knob, not the CPU fix.
            as_of: Issue athenaeum#308 slice 3 — the date the index reflects. The
                inactive filter drops pages outside their
                ``[valid_from, valid_until]`` window relative to THIS date.
                ``None`` (default) means today, so the live index is
                unchanged. Pass a past date to build an as-of *rewind*
                index: a page whose ``valid_until`` had not yet passed on
                that date is included even if it has expired since. An as-of
                build is always a FULL build (a historical snapshot has no
                stable manifest to diff against), written into whatever
                ``cache_dir`` the caller chose (a scratch dir, so the live
                index is untouched).
            full_rehash_max_age_days: Issue athenaeum#373 — the self-healing backstop for
                the athenaeum#370 stat pre-filter. On an INCREMENTAL build, when the
                manifest has not recorded a full re-hash within this many days,
                the stat fast-path is skipped for ONE build: every file is
                re-read and re-hashed so a content edit that preserved both
                ``mtime`` and ``size`` is finally caught. The change delta is
                STILL applied incrementally (no full re-embed / FTS5 rebuild).
                ``0`` / negative = always re-hash; a very large value =
                effectively never. Ignored on a full or as-of build (both
                already re-hash everything).
            config: Issue athenaeum#532 — the resolved ``athenaeum.yaml`` config, used to
                honor the storage-adapter corpus policy: a page whose entity
                class (wiki ``type:``) routes to a surface with ``embedded:
                false`` is dropped from the index at scan time, the same way
                a ``pii:``-flagged page (athenaeum#427) is. ``None`` (default) preserves
                the pre-athenaeum#532 behavior — every page is indexed — and is what the
                shell-hook convenience builders pass. A no-op for the default
                configuration (every class maps to the all-true wiki surface).

        Returns the total number of pages in the index across all roots.
        """
        ...

    def query(
        self,
        query: str,
        cache_dir: Path,
        *,
        n: int = 5,
        exclude: set[str] | None = None,
        wiki_root: Path | None = None,
        caller_audience: set[str] | None = None,
        as_of: date | None = None,
        type_filter: str | Sequence[str] | None = None,
    ) -> list[tuple[str, str, float]]:
        """Search the index.

        ``wiki_root`` is used by scan-on-query backends (e.g. keyword) that
        don't maintain an on-disk index; indexed backends ignore it.

        ``as_of`` (issue athenaeum#308 slice 3) pins the temporal view. Indexed
        backends (fts5 / vector) filter at BUILD time, so they IGNORE this
        parameter — an as-of view for them is a matching as-of index (see
        ``build_index``). The scan-on-query ``keyword`` backend honors it
        directly, filtering each page against its validity window at query
        time. ``None`` (default) means today.

        ``caller_audience`` (issue athenaeum#312) pins the query to a restricted read
        scope. ``None`` is the owner / default caller: no filtering, every
        page (untagged included) is eligible. A non-None set restricts the
        result to pages the caller is authorized for, with the audience
        predicate pushed INSIDE the backend query so BM25/kNN top-k is
        computed over permitted rows only — a forbidden page can neither
        occupy a slot nor push a permitted page past the limit. Fail-closed:
        untagged / malformed pages are withheld from a restricted caller.

        ``type_filter`` (issue athenaeum#964) narrows the search to one or more
        entity classes (a page's ``type:``, resolved via
        :func:`athenaeum.models.resolve_page_type`). Accepts a single class
        name or a sequence of names (OR semantics — a page matching ANY named
        class is eligible); ``None`` (default) or an empty value applies no
        filter, byte-identical to the pre-athenaeum#964 behavior. The value is an
        OPAQUE operator-defined string — it is NEVER validated against
        ``wiki/_schema/types.md`` here (see :mod:`athenaeum.entity_schema` for
        the declared/observed registry a caller can consult before choosing
        one). Every backend MUST push this predicate INSIDE the query (before
        ranking/top-k is selected), exactly like the ``caller_audience``
        predicate above — a backend that only post-filters the result list
        returns silently-wrong (too-few or wrongly-ranked) answers, per this
        Protocol's existing invariant for ``caller_audience``.

        Returns a list of ``(filename, page_name, score)`` tuples,
        ordered by relevance (best first). The ``filename`` may be a
        bare name (wiki entry) or ``<root_name>/<relpath>`` (extra-root
        entry) — callers resolving to a filesystem path must try each
        configured root in turn.
        """
        ...


# ---------------------------------------------------------------------------
# FTS5 backend
# ---------------------------------------------------------------------------

# Public stopword list — sorted tuple for deterministic CLI output.
# Exposed as the single source of truth so shell hooks and downstream
# callers don't re-hardcode their own copy. See `athenaeum stopwords`
# CLI subcommand and examples/claude-code/user-prompt-recall.sh.
STOPWORDS: tuple[str, ...] = tuple(
    sorted(
        set(
            "the and for are but not you all can had her was one our out has his how "
            "its let may new now old see way who did get got him she too use with from "
            "have this that they will been call come each find give help here just know "
            "like long look make many more most much must next only over said same some "
            "such take tell than them then very want well went were what when which "
            "while work also back been being both came does done down even goes going "
            "good keep last left life line made need never part place point right show "
            "small still think those turn used using where would about after again "
            "could every great might often other shall should since start state still "
            "there these thing think three through under until which while world would "
            "years your into just like made over said some than them then time very "
            "want what when will with year does really right going being looking "
            "trying running check please sure okay yeah thanks".split()
        )
    )
)

# Stopwords stripped before building an FTS5 query.
_STOPWORDS: frozenset[str] = frozenset(STOPWORDS)

_DB_NAME = "wiki-index.db"

# Filenames excluded from the intake scan. ``MEMORY.md`` is the per-scope
# curated index file generated by ``scripts/build-per-scope-memory-index.py``
# — we don't want it appearing as a recall hit because it's a table of
# contents, not a memory. Callers who want to search index files directly
# can do so with a filename-targeted query outside recall.
_INTAKE_SKIP_NAMES: frozenset[str] = frozenset({"MEMORY.md"})


def normalize_type_filter(
    type_filter: str | Sequence[str] | None,
) -> tuple[str, ...] | None:
    """Normalize a ``query(type_filter=...)`` argument to a dedup'd tuple.

    Issue athenaeum#964: accepts a single class name or a sequence, per the
    ``SearchBackend.query`` contract. Returns ``None`` when the input is
    ``None`` or normalizes to nothing (an empty string, an empty sequence, or
    a sequence of only blank strings) — the "no filter" case every backend
    must treat identically to the parameter being absent. Values are
    trimmed but NOT case-folded or otherwise validated: the filter is an
    opaque operator-defined string (see the Protocol docstring).
    """
    if type_filter is None:
        return None
    values = [type_filter] if isinstance(type_filter, str) else list(type_filter)
    normalized = tuple(dict.fromkeys(v.strip() for v in values if v and v.strip()))
    return normalized or None


def _like_escape(value: str) -> str:
    """Escape SQL ``LIKE`` wildcards in an audience role id (issue athenaeum#312).

    Role ids are operator-controlled, but a stray ``%`` / ``_`` in a role would
    turn the delimiter-anchored ``LIKE`` predicate into an unintended wildcard.
    Escaped with a backslash to pair with ``ESCAPE '\\'`` in the query.
    """
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _extract_frontmatter_fields(text: str) -> tuple[str, str, str, str]:
    """Parse ``name / tags / aliases / description`` from YAML frontmatter.

    Returns a 4-tuple of strings (empty when not present). Mirrors the
    hand-rolled parser FTS5 used inline — factored out so the intake-root
    scanner shares one implementation.
    """
    name, tags, aliases, description = "", "", "", ""
    if not text.startswith("---"):
        return name, tags, aliases, description
    end = text.find("---", 4)
    if end <= 0:
        return name, tags, aliases, description
    fm = text[4:end]
    in_description = False
    for raw_line in fm.splitlines():
        # Issue athenaeum#1324: PyYAML folds a plain scalar longer than ~80
        # columns onto indented continuation lines, so a ``description:``
        # written by ``render_frontmatter`` may span several. Join them back
        # (a continuation is any indented line following the key) so the
        # index holds the whole sentence, not its first 80 characters.
        if in_description and raw_line[:1] in (" ", "\t") and raw_line.strip():
            description = f"{description} {raw_line.strip()}"
            continue
        in_description = False
        line = raw_line.strip()
        if line.startswith("name:"):
            name = line[5:].strip().strip("\"'")
        elif line.startswith("tags:"):
            tags = line[5:].strip().strip("[]")
        elif line.startswith("aliases:"):
            aliases = line[8:].strip().strip("[]")
        elif line.startswith("description:"):
            description = line[12:].strip()
            in_description = True
    # Strip the YAML quote delimiters ONCE, over the fully joined value — a
    # quoted scalar's closing quote sits on its LAST folded line, and
    # stripping per line would also eat a legitimate apostrophe ending an
    # interior line.
    description = description.strip("\"'")
    return name, tags, aliases, description


def _wiki_relpath_excluded(rel: Path) -> bool:
    """True when *rel* (a path relative to ``wiki_root``) is excluded from
    recall/indexing by the underscore-prefix convention.

    Issue athenaeum#898: the documented convention below ("underscore-prefixed
    files are excluded") was, in every RECURSIVE ``wiki_root`` walk, actually
    implemented as "underscore-prefixed *filenames* are excluded" — a check
    on ``path.name`` only. That is exactly right for a FLAT scan (there are
    no subdirectories to miss), but ``wiki_root`` also holds operational
    subdirectories now: quarantine (``wiki/_quarantine/<source>/<name>.md``,
    athenaeum#898) moves a raw file OUT of compile after it repeatedly exceeded a
    per-file bound, but the file keeps its ORIGINAL basename — so a
    filename-only check let it sail straight through a recursive walk
    unfiltered, meaning a file quarantined *for being poison* stopped being
    compiled but STARTED being served as a recall hit (inverting the whole
    point of quarantine). This checks every path SEGMENT, not just the leaf
    filename, so any current or future ``_``-prefixed operational
    subdirectory under ``wiki_root`` is covered by the same one rule.
    """
    return any(part.startswith("_") for part in rel.parts)


def _iter_wiki_entries(wiki_root: Path) -> Iterable[tuple[str, Path]]:
    """Yield ``(filename, full_path)`` for wiki markdown pages.

    Wiki is a flat shallow scan — underscore-prefixed files are excluded
    (``_index.md``, ``_pending_questions.md``, etc.). Flat, so this never
    needs :func:`_wiki_relpath_excluded`'s directory-segment check — there
    are no subdirectories to walk into in the first place.
    """
    try:
        names = sorted(os.listdir(wiki_root))
    except OSError:
        return
    for fname in names:
        if not fname.endswith(".md") or fname.startswith("_"):
            continue
        if fname in _INTAKE_SKIP_NAMES:
            continue
        yield fname, wiki_root / fname


def _iter_extra_root_entries(
    extra_roots: Iterable[Path] | None,
) -> Iterable[tuple[str, Path]]:
    """Yield ``(indexed_filename, full_path)`` for extra intake roots.

    Each extra root is scanned recursively. ``indexed_filename`` is
    ``<root_name>/<relpath_posix>`` so wiki entries (bare name) and extra-
    root entries never collide and remain distinguishable to the recall
    formatter. Non-``.md`` files and ``MEMORY.md`` are excluded; the
    ``_unscoped/`` subdirectory is included (its files are first-class
    memories, not metadata). Missing roots are silently skipped — this
    is intake code and shouldn't crash on an unconfigured knowledge base.
    """
    if not extra_roots:
        return
    for root in extra_roots:
        if not root.is_dir():
            continue
        root_name = root.name
        for path in sorted(root.rglob("*.md")):
            if path.name in _INTAKE_SKIP_NAMES:
                continue
            try:
                rel = path.relative_to(root).as_posix()
            except ValueError:
                continue
            yield f"{root_name}/{rel}", path


def _scan_all_entries(
    wiki_root: Path,
    extra_roots: Iterable[Path] | None,
) -> Iterable[tuple[str, Path]]:
    """Yield every indexable ``(filename, full_path)`` pair.

    Wiki entries come first (bare filename) followed by extra-root
    entries (``<root_name>/<relpath>``). Callers ingest whatever order
    this yields — ordering within each source is alphabetical so index
    rebuilds are deterministic for test assertions.
    """
    yield from _iter_wiki_entries(wiki_root)
    yield from _iter_extra_root_entries(extra_roots)


# ---------------------------------------------------------------------------
# Incremental indexing helpers (issue athenaeum#348)
# ---------------------------------------------------------------------------
#
# Both indexed backends persist a per-page WHOLE-FILE content hash in a JSON
# sidecar manifest next to their index artifact. On rebuild they diff the
# current files against the stored hashes and apply only the delta — add
# new, re-index changed, delete removed. Hashing the whole file (frontmatter
# + body) means a frontmatter-only change (e.g. issue athenaeum#312 audience) is
# caught just as a body edit is; a body-only hash would miss it. Inactive
# memories (issue athenaeum#191) are filtered out BEFORE hashing, so a page that flips
# to inactive drops out of the manifest and is treated as a deletion.

# Manifest sidecar filenames (co-located with each backend's index artifact).
_FTS5_MANIFEST = "fts5-manifest.json"
_VECTOR_MANIFEST = "vector-manifest.json"

# Issue athenaeum#373: ``_DEFAULT_FULL_REHASH_MAX_AGE_DAYS`` (defined above the Protocol)
# bounds the stat pre-filter's blind window — an incremental build that has not
# re-hashed everything within that many days ignores the stat fast-path for ONE
# build (re-reads + re-hashes every file) while still applying the change delta
# incrementally, so a content edit preserving both mtime and size is caught
# without paying for a full re-embed.

# Top-level manifest key recording the epoch-seconds timestamp of the last build
# that re-hashed every file (a full rebuild or a stale-triggered incremental
# re-hash). Absent (a pre-athenaeum#373 manifest) => treated as infinitely stale, so the
# first build after this ships does one full re-hash and stamps it.
_MANIFEST_REHASH_KEY = "last_full_rehash_at"

# Issue athenaeum#977 (S2 of the whole-store adapter design, athenaeum#911): the manifest's
# top-level ``version`` schema stamp for the opaque-version-token ``stats``
# shape (design note §6.2 D3). A manifest whose ``version`` is not this value
# — including every pre-athenaeum#977 manifest (v1 hashes-only, or the old v2
# ``[mtime_ns, size, valid_until]`` shape) — is schema-incompatible: it is
# treated exactly like a v1 manifest (no usable stats), forcing ONE full
# re-hash on the next build (the same mechanism issue athenaeum#373's periodic
# backstop already models), after which the write path stamps this version.
_STORE_STATS_SCHEMA_VERSION = 3


def _now() -> float:
    """Return the current epoch seconds.

    A module-level indirection (not cached) so tests can monkeypatch the clock
    to age a manifest past the full-re-hash staleness window (issue athenaeum#373).
    """
    return time.time()


# Default embedding model (issue athenaeum#315 slice). Kept as the documented default;
# the one-time seed re-embed that incremental seeding requires is the natural
# opportunity to evaluate a stronger model (see VectorBackend).
# TODO(athenaeum#315): when seeding the hash-indexed collection from scratch, evaluate a
# stronger embedding model here and record the eval result before changing the
# default — the seed re-embed is paid once, so it is the cheap moment to swap.
DEFAULT_EMBEDDING_MODEL = "all-MiniLM-L6-v2"


def _passes_globs(
    indexed_name: str,
    include_globs: Iterable[str] | None,
    exclude_globs: Iterable[str] | None,
) -> bool:
    """Return True if ``indexed_name`` survives the include/exclude globs.

    Default (both ``None``) indexes everything. ``include_globs`` is an
    allow-list (the name must match at least one); ``exclude_globs`` is a
    deny-list applied after. Globs match the indexed name — the bare
    filename for wiki entries or ``<root_name>/<relpath>`` for extra roots.
    """
    if include_globs:
        include = list(include_globs)
        if include and not any(fnmatch(indexed_name, g) for g in include):
            return False
    if exclude_globs:
        for g in exclude_globs:
            if fnmatch(indexed_name, g):
                return False
    return True


def _is_recall_inactive(meta: dict[str, Any] | None, as_of: date | None = None) -> bool:
    """Recall-time inactive predicate (issue athenaeum#904) — the SAME as
    :func:`athenaeum.models.is_inactive_memory` EXCEPT a ``bucket: daily``
    page's expired ``valid_until`` does NOT make it inactive here.

    **Why this cannot just be** ``is_inactive_memory``: that predicate is the
    single shared gate for BOTH recall visibility (this module's three
    backends) AND C3 merge-compile member-activeness
    (:meth:`athenaeum.models.AutoMemoryFile.is_inactive`) — the athenaeum#308 doc's
    own words are "so they stay in lockstep". AC4's "deprioritizes, does not
    filter" needs an expired ``daily``-bucket PAGE to remain recall-visible;
    but an expired ``daily``-bucket raw MEMBER must still stop contributing
    new content to the compiled page — that is what "a rapidly-overwritten
    daily status collapses to its latest value" (the issue's own framing)
    means. Loosening the SHARED predicate would do both at once and
    silently resurrect stale content into compiled pages, which nothing in
    the athenaeum#904 design brief asks for. So this is a SEPARATE, local predicate,
    used ONLY by this module's recall query/build gates
    (:meth:`KeywordBackend.query`, :func:`_scan_indexed_records`) — never by
    :meth:`athenaeum.models.AutoMemoryFile.is_inactive`, which keeps calling
    ``is_inactive_memory`` completely unchanged. Deliberately built from
    only :mod:`athenaeum.models`' PRE-EXISTING primitives
    (:func:`~athenaeum.models.parse_superseded_by`,
    :func:`~athenaeum.models.parse_deprecated`,
    :func:`~athenaeum.models.valid_until_expired`) plus a raw ``bucket``
    frontmatter read, rather than importing a models-layer helper — the
    "recall gate, not a general validity concept" scoping belongs at this
    layer, not L1.

    A ``bucket: daily`` page that instead flows through here is picked up by
    recall's currency-aware ranking
    (:func:`athenaeum.mcp_server._is_deprioritized_for_currency`), which
    ranks it lower rather than hiding it — the deterministic sweep
    (:mod:`athenaeum.decay_sweep`) is what eventually removes it from the
    live tree, on the operator's own cadence, never recall itself.

    ``weekly``/``durable``/unbucketed pages are BYTE-IDENTICAL to
    ``is_inactive_memory`` here — the divergence fires ONLY for
    ``bucket: daily``, which is what keeps "a corpus with no buckets
    anywhere is completely unaffected" true for this function too.

    Known limitation (documented, not silently accepted): the FTS5/vector
    incremental-rebuild stat fast-path (:func:`_scan_indexed_records`)
    re-checks an UNCHANGED page's stored ``valid_until`` without a full
    re-read, and that stored stat record does not carry ``bucket`` — so a
    ``daily``-bucket page whose ``valid_until`` crosses ``as_of`` between
    incremental rebuilds can drop out of those TWO indexed backends until
    the next FULL rebuild (the existing periodic
    ``full_rehash_max_age_days`` self-healing backstop picks it back up,
    deprioritized rather than dropped, same as everywhere else). The
    scan-on-query ``keyword`` backend has no such lag — it always sees this
    function's live answer.
    """
    if not meta:
        return False
    if parse_superseded_by(meta):
        return True
    if parse_deprecated(meta):
        return True
    if meta.get("bucket") == "daily":
        return False
    return valid_until_expired(meta, as_of)


def _scan_surface(
    store: FilesystemStore,
    surface: str,
    *,
    keep: Callable[[str], bool],
    prior_versions: Mapping[str, str] | None = None,
) -> tuple[dict[str, ObjectMeta], Mapping[StoreKey, bytes]]:
    """List *surface* once and batch-read only the changed/added subset.

    Issue athenaeum#977 (S2), design note §3.5 P1/P3: :meth:`Store.iter_meta`
    replaces the former per-page ``path.stat()`` with ONE listing call
    regardless of corpus size ``N``, and :meth:`Store.read_many` replaces the
    former per-page ``path.read_bytes()`` with ONE batched read of exactly
    the ``c`` keys whose version differs from *prior_versions* (or has none)
    — never ``N`` individual reads. *keep* is applied INSIDE the listing
    loop, before a key can enter the read set, so a filtered-out object
    (wrong extension, an operational subdirectory, a skip-listed name) is
    never fetched — matching the pre-athenaeum#977 walk, which filtered before
    ever calling ``stat``/``read`` on a page.

    Returns ``(current, contents)``: *current* is ``{relative_key:
    ObjectMeta}`` for every KEPT object; *contents* is ``read_many``'s result
    for the changed/added subset (``{}`` — no ``read_many`` call at all — when
    nothing changed, the common no-op-rebuild case).
    """
    current: dict[str, ObjectMeta] = {}
    to_read: list[StoreKey] = []
    versions = prior_versions or {}
    for meta in store.iter_meta(surface):
        rel = meta.key.key
        if not keep(rel):
            continue
        current[rel] = meta
        if versions.get(rel) != meta.version:
            to_read.append(meta.key)
    contents = store.read_many(to_read) if to_read else {}
    return current, contents


def _scan_indexed_records(
    wiki_root: Path,
    extra_roots: Iterable[Path] | None,
    *,
    include_globs: Iterable[str] | None = None,
    exclude_globs: Iterable[str] | None = None,
    as_of: date | None = None,
    prior: dict[str, tuple[str, str, str]] | None = None,
    config: dict[str, Any] | None = None,
) -> Iterator[tuple[str, Path, str, str, dict[str, Any], tuple[str, str]]]:
    """Yield ``(indexed_name, path, hash, text, meta, statrec)`` per active page.

    Issue athenaeum#977 (S2 of the whole-store adapter design, athenaeum#911): this scan
    is built on the ``Store`` bulk primitives (design note §6.2) via
    :func:`_scan_surface` instead of a per-page ``path.stat()`` +
    ``path.read_bytes()`` walk. One :class:`~athenaeum.store.FilesystemStore`
    is constructed per call, scoped to *wiki_root* (surface ``"wiki"``, flat —
    a listed key nested under an operational subdirectory such as
    ``_quarantine`` is excluded exactly like the old non-recursive
    ``os.listdir`` walk) plus one synthetic surface per *extra_roots* entry
    (recursive, ``.md``-only, name-prefixed — unchanged from the pre-existing
    convention). No caller-visible signature change: this remains an internal
    helper over filesystem ``Path``s, matching S1's "no callers migrated"
    scoping — the store is an implementation detail of THIS scan, not exposed
    outward. (``_scan_all_entries`` / ``_iter_wiki_entries`` /
    ``_iter_extra_root_entries`` above are left untouched: they also serve
    ``athenaeum.recurring_claims`` and direct test callers that want a plain
    ``Path`` enumeration rather than the store abstraction, so the "which
    files count" rule is necessarily re-expressed here rather than shared.)

    ``content_hash`` is the sha256 of the whole file (frontmatter + body).
    ``text`` is the full decoded file (callers truncate as needed for the
    index document). ``statrec`` is ``(version, valid_until_iso)`` — *version*
    is the store adapter's opaque token (design note §6.2 D3; ``mtime_ns:size``
    for ``FilesystemStore`` — compared for equality only, never parsed).
    Inactive memories are filtered here so they are absent from both index
    and manifest — the incremental differ then treats an active→inactive
    flip as a deletion. Unreadable/vanished files are skipped.

    Issue athenaeum#427: a page carrying a truthy ``pii:`` frontmatter flag (see
    :func:`athenaeum.pii.is_pii_flagged`) is ALSO filtered out here, same as
    an inactive memory — belt-and-suspenders exclusion for PII inline in
    narrative on a page an operator has not (or not yet, athenaeum#437) moved to the
    excluded storage surface. It never enters the index or the manifest, so a
    page later un-flagged picks back up on the next incremental build exactly
    like a re-activated memory would.

    ``as_of`` (issue athenaeum#308 slice 3) pins the temporal view: a page outside its
    validity window relative to ``as_of`` (default today) is filtered out here,
    exactly like a athenaeum#191 tombstone. Only an as-of BUILD passes this (and an as-of
    build is always full), so the manifest a normal live rebuild diffs against is
    never contaminated by a historical view.

    ``prior`` (issue athenaeum#370, opaque-token schema per athenaeum#977) enables the
    stat pre-filter: a map ``indexed_name -> (version, valid_until_iso, hash)``
    from the last manifest. When a file's CURRENT store version matches its
    prior entry, its body is NOT read or re-hashed — the stored hash is
    reused (rsync-style heuristic). The page was active last build (only
    active pages are in the manifest) and its content is unchanged, so it
    stays active EXCEPT if its ``valid_until`` has since expired relative to
    ``as_of`` — that time-varying bound is re-checked from the stored date
    without a read, preserving the athenaeum#308 date-expiry semantics.
    Version-matched rows yield placeholder ``text=""``/``meta={}``: callers
    only consume those for the add/change delta, whose members always fail
    the version match and are freshly read (via one batched ``read_many``
    call per surface — design note P3 — never one read per file). ``prior``
    MUST be ``None``/empty for a full (re)build — a full build inserts every
    scanned record, so the placeholders would corrupt it. Callers pass
    ``prior`` only on the incremental-apply path.
    """
    include = list(include_globs) if include_globs else None
    exclude = list(exclude_globs) if exclude_globs else None
    prior_map = prior or {}

    roots: dict[str, Path] = {"wiki": wiki_root}
    extra_surfaces: list[tuple[str, str]] = []  # (surface_name, indexed-name prefix)
    for i, root in enumerate(extra_roots or ()):
        if not root.is_dir():
            continue
        surface = f"extra{i}"
        roots[surface] = root
        extra_surfaces.append((surface, root.name))
    store = FilesystemStore(knowledge_root=wiki_root, roots=roots)

    def _local_path_for(key: StoreKey) -> Path:
        fn = store.capabilities.local_path_for
        assert fn is not None  # FilesystemStore always declares this escape hatch
        return fn(key)

    def _decode(
        indexed_name: str,
        key: StoreKey,
        meta_obj: ObjectMeta,
        contents: Mapping[StoreKey, bytes],
    ) -> tuple[str, Path, str, str, dict[str, Any], tuple[str, str]] | None:
        prior_rec = prior_map.get(indexed_name)
        if prior_rec is not None and prior_rec[0] == meta_obj.version:
            # Version fast-path: content unchanged since the last active build.
            # Re-check ONLY the time-varying upper bound (superseded_by /
            # deprecated are content-based and cannot change without a version
            # change). ``valid_until`` may have crossed ``as_of`` (default
            # today) with no content edit — drop the page then so it becomes a
            # manifest ``removed`` and leaves the index (issue athenaeum#308).
            stored_vu = prior_rec[1]
            if stored_vu and valid_until_expired({"valid_until": stored_vu}, as_of):
                return None
            return (
                indexed_name,
                _local_path_for(key),
                prior_rec[2],
                "",
                {},
                (meta_obj.version, stored_vu),
            )
        data = contents.get(key)
        if data is None:  # pragma: no cover - defensive: vanished between list and read
            return None
        content_hash = hashlib.sha256(data).hexdigest()
        text = data.decode("utf-8", errors="replace")
        meta, _ = parse_frontmatter(text)
        # Issue athenaeum#191: inactive members never enter the index or the manifest.
        # Issue athenaeum#308: an as-of build additionally drops pages outside their
        # validity window relative to ``as_of`` (default today). Issue athenaeum#904:
        # ``_is_recall_inactive`` (not ``is_inactive_memory``) so an expired
        # ``bucket: daily`` page stays indexed for currency ranking instead
        # of being hard-dropped — see that function's docstring.
        if _is_recall_inactive(meta, as_of):
            return None
        # Issue athenaeum#427: a ``pii: true``-flagged page never enters the index or
        # the manifest (belt-and-suspenders — see the docstring above).
        if is_pii_flagged(meta):
            return None
        # Issue athenaeum#532 (H4): honor the storage-adapter corpus policy at index
        # build. A page whose entity class routes to a surface with
        # ``embedded: false`` never enters the FTS5 / vector store — the
        # ``embedded`` capability the storage contract promises, enforced the
        # same way athenaeum#427 excludes PII and ``wiki_dedupe`` drops
        # non-``merge_eligible`` classes. NO-OP by default: with no ``storage:``
        # config every class maps to the all-true wiki surface, so
        # ``is_embedded`` is ``True`` for every page and nothing is dropped.
        # ``config is None`` (callers that don't thread config, e.g. shell-hook
        # convenience builds) also short-circuits to today's behavior.
        if config is not None:
            page_type = str(meta.get("type") or "")
            if not is_embedded(page_type, config):
                return None
        vu = validity_bound_str(meta, "valid_until")
        return indexed_name, _local_path_for(key), content_hash, text, meta, (meta_obj.version, vu)

    # -- wiki: flat surface, .md only, no underscore/skip-listed names ------
    def _wiki_keep(rel: str) -> bool:
        if "/" in rel or not rel.endswith(".md") or rel.startswith("_"):
            return False
        if rel in _INTAKE_SKIP_NAMES:
            return False
        return _passes_globs(rel, include, exclude)

    wiki_prior_versions = {
        name: rec[0] for name, rec in prior_map.items() if "/" not in name
    }
    wiki_current, wiki_contents = _scan_surface(
        store, "wiki", keep=_wiki_keep, prior_versions=wiki_prior_versions
    )
    for rel in sorted(wiki_current):
        record = _decode(rel, wiki_current[rel].key, wiki_current[rel], wiki_contents)
        if record is not None:
            yield record

    # -- extra roots: recursive, .md only, name-prefixed --------------------
    for surface, root_name in extra_surfaces:
        name_prefix = root_name + "/"

        def _extra_keep(rel: str, _root_name: str = root_name) -> bool:
            if not rel.endswith(".md"):
                return False
            if Path(rel).name in _INTAKE_SKIP_NAMES:
                return False
            return _passes_globs(f"{_root_name}/{rel}", include, exclude)

        surface_prior_versions = {
            name[len(name_prefix) :]: rec[0]
            for name, rec in prior_map.items()
            if name.startswith(name_prefix)
        }
        extra_current, extra_contents = _scan_surface(
            store, surface, keep=_extra_keep, prior_versions=surface_prior_versions
        )
        for rel in sorted(extra_current):
            indexed_name = f"{root_name}/{rel}"
            record = _decode(
                indexed_name, extra_current[rel].key, extra_current[rel], extra_contents
            )
            if record is not None:
                yield record


def _compute_delta(
    current_hashes: dict[str, str],
    stored_hashes: dict[str, str],
) -> tuple[list[str], list[str], list[str]]:
    """Return ``(added, changed, removed)`` indexed-name lists.

    ``added``: present now, absent from the manifest.
    ``changed``: present in both, whole-file hash differs.
    ``removed``: in the manifest, absent now (deleted or gone inactive).
    """
    added = [k for k in current_hashes if k not in stored_hashes]
    changed = [
        k
        for k, h in current_hashes.items()
        if k in stored_hashes and stored_hashes[k] != h
    ]
    removed = [k for k in stored_hashes if k not in current_hashes]
    return added, changed, removed


def _load_manifest(path: Path) -> dict[str, Any] | None:
    """Load a manifest sidecar, or ``None`` when absent/unreadable/malformed."""
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def _manifest_hashes(manifest: dict[str, Any] | None) -> dict[str, str]:
    """Extract the ``{indexed_name: hash}`` map from a loaded manifest."""
    if not manifest:
        return {}
    hashes = manifest.get("hashes")
    if isinstance(hashes, dict):
        return {str(k): str(v) for k, v in hashes.items()}
    return {}


def _manifest_stats(manifest: dict[str, Any] | None) -> dict[str, tuple[str, str]]:
    """Extract the ``{indexed_name: (version, valid_until)}`` stat map.

    Issue athenaeum#370's stat pre-filter, re-keyed on the opaque store-adapter
    version token by issue athenaeum#977 (design note §6.2 D3: ``version`` is
    compared for equality only, never parsed — for ``FilesystemStore`` today
    it happens to be ``mtime_ns:size``, but callers must not assume that
    shape). ``manifest["version"]`` is this schema's stamp
    (``_STORE_STATS_SCHEMA_VERSION``): a manifest that does not carry it —
    every pre-athenaeum#977 manifest, whether v1 (hashes-only) or the old v2
    ``[mtime_ns, size, valid_until]`` shape — is schema-incompatible and
    returns ``{}`` here exactly like a v1 manifest always has, which forces a
    one-time full re-hash of every file (the same forced-rehash backstop
    issue athenaeum#373 already models) and the manifest upgrades to the current
    schema on the next write. Each stored entry is a ``[version,
    valid_until]`` list (JSON has no tuples); rows that do not parse are
    skipped individually (fail to a re-hash for that one file), never
    crashing the build.
    """
    if not manifest or manifest.get("version") != _STORE_STATS_SCHEMA_VERSION:
        return {}
    stats = manifest.get("stats")
    if not isinstance(stats, dict):
        return {}
    out: dict[str, tuple[str, str]] = {}
    for name, rec in stats.items():
        try:
            version, vu = rec[0], rec[1]
            out[str(name)] = (str(version), str(vu or ""))
        except (TypeError, ValueError, IndexError):
            continue
    return out


def _manifest_last_full_rehash(manifest: dict[str, Any] | None) -> float | None:
    """Read the manifest's ``last_full_rehash_at`` epoch seconds (issue athenaeum#373).

    ``None`` when absent (a pre-athenaeum#373 manifest) or malformed — the caller treats
    that as infinitely stale and forces one full re-hash. A ``bool`` (a subclass
    of ``int``) is rejected so a stray ``true`` cannot read as ``1.0``.
    """
    if not manifest:
        return None
    raw = manifest.get(_MANIFEST_REHASH_KEY)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    return float(raw)


def _scan_prior(
    manifest: dict[str, Any] | None,
) -> dict[str, tuple[str, str, str]]:
    """Join a manifest's hashes + stats into the scan's ``prior`` map (athenaeum#370).

    Returns ``{indexed_name: (version, valid_until, hash)}`` for names that
    have BOTH a hash and a stat entry — unchanged from athenaeum#370's join, just
    keyed on the opaque store-adapter version token (athenaeum#977) instead of a raw
    ``(mtime_ns, size)`` pair. A name missing either (e.g. every name in a
    schema-incompatible manifest, which ``_manifest_stats`` reports as having
    no ``stats``) is omitted, so it is read and re-hashed exactly once — after
    which the write records its stat in the current schema.
    """
    hashes = _manifest_hashes(manifest)
    stats = _manifest_stats(manifest)
    out: dict[str, tuple[str, str, str]] = {}
    for name, (version, vu) in stats.items():
        h = hashes.get(name)
        if h is not None:
            out[name] = (version, vu, h)
    return out


def _write_manifest(
    path: Path,
    hashes: dict[str, str],
    extra: dict[str, Any] | None = None,
    stats: dict[str, tuple[str, str]] | None = None,
    last_full_rehash_at: float | None = None,
) -> None:
    """Atomically write the manifest sidecar (temp file + rename).

    ``stats`` (issue athenaeum#370, opaque-token schema per athenaeum#977) persists the
    per-file ``(version, valid_until)`` alongside the hash so the next
    build's stat pre-filter can skip re-reading unchanged files. Bumped to
    ``version: _STORE_STATS_SCHEMA_VERSION`` when stats are written; a reader
    that only knows ``hashes`` is unaffected (still present).

    ``last_full_rehash_at`` (issue athenaeum#373) records the epoch seconds of the most
    recent build that re-hashed every file (a full rebuild, a stale-triggered
    incremental re-hash, or a schema-stamp mismatch — athenaeum#977). The
    stale-detection backstop reads it to decide when to force the next full
    re-hash; a fresh incremental build PRESERVES the prior value by passing it
    back unchanged. ``None`` omits the key.
    """
    version = _STORE_STATS_SCHEMA_VERSION if stats is not None else 1
    payload: dict[str, Any] = {"version": version, "hashes": hashes}
    if stats is not None:
        payload["stats"] = {k: [v[0], v[1]] for k, v in stats.items()}
    if last_full_rehash_at is not None:
        payload[_MANIFEST_REHASH_KEY] = last_full_rehash_at
    if extra:
        payload.update(extra)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(path)


class FTS5Backend:
    """SQLite FTS5 full-text search with BM25 ranking and porter stemming."""

    # Issue athenaeum#530 (M7): on-disk schema version, stamped into the SQLite DB via
    # ``PRAGMA user_version`` at build time and checked before every incremental
    # build. Bump this whenever the ``wiki`` table shape changes (column set,
    # order, or tokenizer). A DB whose stamp does not match — including a legacy
    # DB that predates the stamp (``user_version`` defaults to 0) — is force-
    # rebuilt instead of reused, so a stale-shaped table can never survive an
    # incremental build and turn every audience-filtered query into a silent
    # ``OperationalError`` → empty recall. Version 2 == the ``audience``-aware
    # shape (athenaeum#312); version 3 == the ``type``-aware shape (issue athenaeum#964,
    # AC amendment 1); version 4 == the ``memory_tier``-aware shape (issue
    # athenaeum#1120) — the index-carried tier verdict the unprompted-recall
    # shell hook filters on directly (``memory_tiers.resolve_tier`` runs once
    # here, at build time, so the hook never has to reimplement tier
    # resolution in shell). Exactly like the version-2/version-3 bumps above,
    # an unchanged page's stat-matched incremental scan never re-reads its
    # frontmatter, so without this bump a contract change ("tier is now
    # filterable") would silently serve the old (missing) column value — i.e.
    # NULL, which never equals ``'hot'`` — for every page an ordinary
    # incremental build leaves untouched, turning tier-filtered recall
    # silently empty for exactly the pages that hadn't changed.
    _SCHEMA_VERSION = 4

    # SQL fragments shared by the full and incremental build paths. ``type``
    # and ``memory_tier`` are both UNINDEXED (out of the BM25 term space,
    # exact-matched via WHERE) — same storage shape ``audience`` established
    # (issue athenaeum#312) and ``type`` followed (issue athenaeum#964).
    _CREATE_SQL = (
        "CREATE VIRTUAL TABLE IF NOT EXISTS wiki USING fts5"
        "(filename, name, tags, aliases, description, audience UNINDEXED, "
        "type UNINDEXED, memory_tier UNINDEXED, "
        'tokenize="porter unicode61")'
    )
    _INSERT_SQL = "INSERT INTO wiki VALUES (?,?,?,?,?,?,?,?)"

    @staticmethod
    def _db_schema_version(db_path: Path) -> int:
        """Read the DB's ``PRAGMA user_version`` (issue athenaeum#530 M7).

        Returns 0 for a missing/unreadable DB or one that was never stamped —
        both of which must NOT be reused for an incremental build.
        """
        try:
            conn = sqlite3.connect(str(db_path))
        except sqlite3.Error:
            return 0
        try:
            row = conn.execute("PRAGMA user_version").fetchone()
            return int(row[0]) if row else 0
        except sqlite3.Error:
            return 0
        finally:
            conn.close()

    def _stamp_schema_version(self, conn: sqlite3.Connection) -> None:
        """Stamp the current schema version into the DB (issue athenaeum#530 M7).

        ``PRAGMA user_version`` does not accept bound parameters, so the value
        is interpolated — safe because it is our own integer class constant.
        """
        conn.execute(f"PRAGMA user_version = {int(self._SCHEMA_VERSION)}")

    @staticmethod
    def _row_for(
        indexed_name: str,
        path: Path,
        text: str,
        meta: dict[str, Any],
        *,
        config: dict[str, Any] | None = None,
    ) -> tuple[str, str, str, str, str, str, str, str]:
        """Build the FTS5 row tuple for one page."""
        name, tags, aliases, description = _extract_frontmatter_fields(text)
        if not name:
            # For extra-root entries use the leaf stem (not the prefixed
            # indexed_name) so recall results show a clean title.
            name = path.stem
        # Issue athenaeum#312: store each page's effective audience (delimited,
        # anchored) so Layer B can filter inside the query.
        audience = audience_index_string(meta)
        # Issue athenaeum#964: unlike name/tags/aliases/description above (the
        # hand-rolled ``_extract_frontmatter_fields`` scanner), ``type`` is read
        # from the REAL ``parse_frontmatter`` result the caller already has —
        # ``meta`` — via the one shared precedence resolver, so a page using
        # either the top-level or nested ``metadata:`` shape is found the
        # same way regardless of which scanner produced ``meta``.
        page_type = resolve_page_type(meta)
        # Issue athenaeum#1120: resolve the retrieval-cost tier ONCE here, at
        # index-build time, and store the verdict so the unprompted-recall
        # shell hook can filter ``WHERE memory_tier = 'hot'`` directly instead
        # of reimplementing ``memory_tiers.resolve_tier``'s cascade in shell.
        # Function-local import: ``athenaeum.memory_tiers`` is an L4
        # domain/pipeline module and this module's docstring states L3 never
        # imports L4 at module scope (mirrors the pattern ``resolve_tier``
        # itself already uses for its own ``athenaeum.storage`` import).
        from athenaeum.memory_tiers import resolve_tier

        memory_tier = resolve_tier(meta, config=config)
        return (
            indexed_name,
            name,
            tags,
            aliases,
            description,
            audience,
            page_type,
            memory_tier,
        )

    def build_index(
        self,
        wiki_root: Path,
        cache_dir: Path,
        *,
        extra_roots: Iterable[Path] | None = None,
        incremental: bool = True,
        include_globs: Iterable[str] | None = None,
        exclude_globs: Iterable[str] | None = None,
        as_of: date | None = None,
        full_rehash_max_age_days: float = _DEFAULT_FULL_REHASH_MAX_AGE_DAYS,
        config: dict[str, Any] | None = None,
    ) -> int:
        """Scan wiki + extra intake roots and build an FTS5 index.

        See :meth:`SearchBackend.build_index` for the full contract. Wiki
        entries are indexed with a bare filename; extra-root entries with
        ``<root_name>/<relpath>``. Incremental by default (issue athenaeum#348):
        only added/changed/removed pages are touched, keyed off a whole-file
        content-hash manifest sidecar. An as-of build (``as_of`` set, issue
        athenaeum#308) is always a full build reflecting that date's validity windows.
        ``full_rehash_max_age_days`` (issue athenaeum#373) periodically forces a full
        re-hash on the incremental path — see :meth:`SearchBackend.build_index`.
        """
        cache_dir.mkdir(parents=True, exist_ok=True)
        db_path = cache_dir / _DB_NAME
        manifest_path = cache_dir / _FTS5_MANIFEST

        # Issue athenaeum#308: an as-of view is a historical snapshot — never diff it
        # against (or seed) the live manifest, so force a full build.
        stored = (
            _load_manifest(manifest_path) if incremental and as_of is None else None
        )
        # Incremental only when we have BOTH a prior manifest and a live DB;
        # otherwise seed with a clean full rebuild.
        #
        # Issue athenaeum#530 (M7): additionally require the on-disk DB to carry the
        # CURRENT schema version. A DB built by an older athenaeum (e.g. a
        # pre-``audience`` shape, or any DB predating the PRAGMA stamp, which
        # reads back 0) must NOT be reused: ``CREATE VIRTUAL TABLE IF NOT
        # EXISTS`` is a no-op against the old shape, so the positional INSERT
        # mismatches and every audience-filtered query hits a missing column,
        # raises ``OperationalError``, and is silently turned into empty recall.
        # A schema mismatch forces a full rebuild (the DB is unlinked and
        # recreated below), which self-heals a legacy index on the next build.
        db_schema_ok = (
            db_path.is_file()
            and self._db_schema_version(db_path) == self._SCHEMA_VERSION
        )
        if (
            incremental
            and as_of is None
            and stored is not None
            and db_path.is_file()
            and not db_schema_ok
        ):
            import logging

            logging.getLogger(__name__).warning(
                "search: FTS5 index at %s has schema version %d, expected %d — "
                "forcing a full rebuild instead of an incremental one so a "
                "stale-shaped table cannot silently break audience-filtered "
                "recall (issue athenaeum#530)",
                db_path,
                self._db_schema_version(db_path),
                self._SCHEMA_VERSION,
            )
        do_incremental = (
            incremental and as_of is None and stored is not None and db_schema_ok
        )

        # Issue athenaeum#373: self-healing full-re-hash backstop. On the incremental
        # path, if the manifest has not recorded a full re-hash within the max
        # age, force one this build (``prior=None`` => every file re-read and
        # re-hashed) while STILL applying the change delta incrementally. A fresh
        # manifest preserves its stored timestamp; a full rebuild always stamps.
        #
        # Issue athenaeum#977: a manifest whose ``version`` predates the opaque-token
        # schema (``_STORE_STATS_SCHEMA_VERSION``) is folded into the SAME
        # staleness check — ``_manifest_stats``/``_scan_prior`` would already
        # report no usable prior stats for it, but treating it as ``stale``
        # explicitly also stamps a fresh ``last_full_rehash_at`` for the forced
        # re-hash this build performs, instead of silently preserving a
        # timestamp from before the schema changed.
        now = _now()
        last_rehash = _manifest_last_full_rehash(stored)
        schema_mismatch = (
            stored is not None and stored.get("version") != _STORE_STATS_SCHEMA_VERSION
        )
        stale = (
            last_rehash is None
            or schema_mismatch
            or (now - last_rehash) > (full_rehash_max_age_days * 86400.0)
        )
        rehash_at = now if (not do_incremental or stale) else last_rehash

        # Issue athenaeum#370: feed the prior manifest's stats into the scan so unchanged
        # files are stat-matched instead of re-read. A full build inserts every
        # scanned record, so it must read every file — ``prior=None`` there. A
        # stale incremental build (athenaeum#373) likewise passes ``prior=None`` to force
        # a re-hash of every file.
        prior = _scan_prior(stored) if (do_incremental and not stale) else None
        current = list(
            _scan_indexed_records(
                wiki_root,
                extra_roots,
                include_globs=include_globs,
                exclude_globs=exclude_globs,
                as_of=as_of,
                prior=prior,
                config=config,
            )
        )
        current_hashes = {name: h for name, _p, h, _t, _m, _s in current}
        current_stats = {name: s for name, _p, _h, _t, _m, s in current}

        if not do_incremental:
            if db_path.exists():
                db_path.unlink()
            conn = sqlite3.connect(str(db_path))
            try:
                conn.execute(self._CREATE_SQL)
                self._stamp_schema_version(conn)  # issue athenaeum#530 (M7)
                rows = [
                    self._row_for(name, path, text, meta, config=config)
                    for name, path, _h, text, meta, _s in current
                ]
                conn.executemany(self._INSERT_SQL, rows)
                conn.commit()
            finally:
                conn.close()
            _write_manifest(
                manifest_path,
                current_hashes,
                stats=current_stats,
                last_full_rehash_at=rehash_at,
            )
            return len(rows)

        # Incremental path — diff and apply only the delta.
        stored_hashes = _manifest_hashes(stored)
        added, changed, removed = _compute_delta(current_hashes, stored_hashes)

        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute(self._CREATE_SQL)  # defensive: table may predate a wipe
            self._stamp_schema_version(conn)  # issue athenaeum#530 (M7): keep the stamp current
            to_delete = removed + changed
            if to_delete:
                conn.executemany(
                    "DELETE FROM wiki WHERE filename = ?",
                    [(k,) for k in to_delete],
                )
            reindex = set(added) | set(changed)
            if reindex:
                rows = [
                    self._row_for(name, path, text, meta, config=config)
                    for name, path, _h, text, meta, _s in current
                    if name in reindex
                ]
                conn.executemany(self._INSERT_SQL, rows)
            conn.commit()
            total = int(conn.execute("SELECT count(*) FROM wiki").fetchone()[0])
        finally:
            conn.close()
        _write_manifest(
            manifest_path,
            current_hashes,
            stats=current_stats,
            last_full_rehash_at=rehash_at,
        )
        return total

    def query(
        self,
        query: str,
        cache_dir: Path,
        *,
        n: int = 5,
        exclude: set[str] | None = None,
        wiki_root: Path | None = None,
        caller_audience: set[str] | None = None,
        as_of: date | None = None,
        type_filter: str | Sequence[str] | None = None,
    ) -> list[tuple[str, str, float]]:
        """Query the FTS5 index. Returns ``(filename, name, score)`` triples."""
        del wiki_root  # FTS5 reads the pre-built index, not the wiki files
        del as_of  # athenaeum#308: FTS5 filters at build time; as-of view = as-of index
        db_path = cache_dir / _DB_NAME
        if not db_path.is_file():
            return []

        # Tokenize and filter stopwords
        terms = [
            t
            for t in re.split(r"\W+", query.lower())
            if len(t) >= 3 and t not in _STOPWORDS
        ]
        if not terms:
            return []

        # Build FTS5 MATCH expression: "word1" OR "word2" ...
        fts_query = " OR ".join(f'"{t}"' for t in terms[:8])

        # Build exclusion clause
        exclude_clause = ""
        params: list[str] = []
        if exclude:
            placeholders = ", ".join("?" for _ in exclude)
            exclude_clause = f" AND filename NOT IN ({placeholders})"
            params = list(exclude)

        # Issue athenaeum#312 — Layer B: push the audience predicate INTO the WHERE,
        # BEFORE ``ORDER BY rank LIMIT``, so the BM25 top-k is selected from
        # permitted rows only. A forbidden page can neither occupy a slot nor
        # push a permitted page past the LIMIT. ``caller_audience=None`` (owner)
        # adds no predicate — every page is eligible. Each role is a
        # delimiter-anchored, LIKE-escaped, parameterized clause so ``|ops|``
        # never matches ``|opsadmin|`` and role ids can't inject SQL.
        audience_clause = ""
        audience_params: list[str] = []
        if caller_audience is not None:
            # Public marker first (the internal sentinel, escaped so its
            # underscores aren't treated as LIKE wildcards), then one anchored,
            # escaped, parameterized clause per caller role.
            like_clauses = [r"audience LIKE ? ESCAPE '\'"]
            audience_params.append(f"%|{_like_escape(AUDIENCE_PUBLIC_TOKEN)}|%")
            for role in sorted(caller_audience):
                like_clauses.append(r"audience LIKE ? ESCAPE '\'")
                audience_params.append(f"%|{_like_escape(role)}|%")
            audience_clause = " AND (" + " OR ".join(like_clauses) + ")"

        # Issue athenaeum#964: push the type predicate INSIDE the WHERE, same rule
        # ``audience_clause`` above already follows — computed BEFORE
        # ``ORDER BY rank LIMIT`` so a non-matching page can never occupy a
        # top-k slot. ``normalize_type_filter`` returns ``None`` for "no
        # filter", so an unset/blank ``type_filter`` adds no predicate at all
        # (byte-identical to pre-athenaeum#964 behavior).
        type_clause = ""
        type_params: list[str] = []
        normalized_types = normalize_type_filter(type_filter)
        if normalized_types is not None:
            placeholders = ", ".join("?" for _ in normalized_types)
            type_clause = f" AND type IN ({placeholders})"
            type_params = list(normalized_types)

        conn = sqlite3.connect(str(db_path))
        try:
            cursor = conn.execute(
                f"SELECT filename, name, rank FROM wiki "
                f"WHERE wiki MATCH ? {exclude_clause}{audience_clause}{type_clause} "
                f"ORDER BY rank LIMIT ?",
                [fts_query, *params, *audience_params, *type_params, n],
            )
            return [(row[0], row[1], row[2]) for row in cursor.fetchall()]
        except sqlite3.OperationalError:
            return []
        finally:
            conn.close()

    def candidates_by_type(self, cache_dir: Path, entity_type: str) -> list[str]:
        """Return every indexed filename whose ``type`` column equals *entity_type*.

        Issue athenaeum#965 (AC amendment 3): the ENUMERATION primitive's read of
        the converged filterable-metadata store this class builds — the SAME
        ``type UNINDEXED`` column :meth:`query`'s ``type_filter`` predicate
        already applies. A plain indexed ``WHERE``, never routed through FTS5
        ``MATCH``/BM25 ranking — enumeration must not go through query-text
        ranking at all. Returns filenames only, sorted for a deterministic
        base ordering before :mod:`athenaeum.enumeration` applies its own
        sort key: per-page frontmatter (needed for predicate evaluation, the
        caller-named sort key, and output field selection) is read by the
        caller from the resolved on-disk path — this table does not, and per
        the issue's "no new index structures" constraint must not, carry
        arbitrary frontmatter fields.

        A missing/unreadable DB returns ``[]`` rather than raising; the
        caller is responsible for ensuring the index exists via
        :meth:`build_index` first.
        """
        db_path = cache_dir / _DB_NAME
        if not db_path.is_file():
            return []
        try:
            conn = sqlite3.connect(str(db_path))
        except sqlite3.Error:
            return []
        try:
            rows = conn.execute(
                "SELECT filename FROM wiki WHERE type = ? ORDER BY filename",
                (entity_type,),
            ).fetchall()
        except sqlite3.Error:
            return []
        finally:
            conn.close()
        return [str(r[0]) for r in rows]


# ---------------------------------------------------------------------------
# Vector backend (chromadb)
# ---------------------------------------------------------------------------

_VECTOR_DIR = "wiki-vectors"
_VECTOR_COLLECTION = "wiki"
# A build-generation token written into the collection dir on every completed
# build_index (issue athenaeum#489). A long-lived server process reads it before each
# query; when it changes, the process's chromadb SharedSystemClient cache is
# stale (an out-of-process reindex replaced the on-disk collection) and must be
# cleared so the next open re-reads the true on-disk state instead of serving
# degraded/None-yielding results from the pinned old handle.
_VECTOR_GENERATION = ".generation"


class DegradedIndexError(RuntimeError):
    """The vector index returned a degenerate (non-ranked) result set (athenaeum#489).

    A collection that yields every neighbour at an identical distance is not
    ranking — it is a degraded/unavailable index (the pre-reindex failure mode
    that returned six unrelated pages all at ``score: 1.5``). Surfacing this as
    an explicit, actionable error is required by athenaeum#489 so the silent failure —
    confidently-formatted, completely wrong results with no signal — can never
    reach the caller as if it were a real ranked answer.
    """


def _read_generation(vector_dir: Path) -> str | None:
    """Return the collection's build-generation token, or ``None`` if absent."""
    try:
        return (vector_dir / _VECTOR_GENERATION).read_text(encoding="utf-8").strip()
    except OSError:
        return None


def _write_generation(vector_dir: Path) -> None:
    """Stamp a fresh build-generation token into *vector_dir* (issue athenaeum#489).

    Called at the end of every completed build_index. A new random token per
    build guarantees a running server observes the change and re-opens its
    stale handle, even for an incremental rebuild that changed the collection
    in place (same path, same collection name).
    """
    import uuid

    try:
        (vector_dir / _VECTOR_GENERATION).write_text(
            uuid.uuid4().hex, encoding="utf-8"
        )
    except OSError:  # pragma: no cover - best-effort stamp; a missing stamp
        # only degrades to the pre-athenaeum#489 "no auto-reopen" behaviour, never worse.
        pass


def _hits_from_query_results(
    results: dict[str, Any],
    n: int,
    caller_audience: set[str] | None,
    *,
    type_narrowed: bool = False,
) -> list[tuple[str, str, float]]:
    """Turn a chromadb ``collection.query`` result into ranked recall hits.

    Pure/deterministic so it is unit-testable without chromadb (issue athenaeum#489).
    Two hardenings over the pre-athenaeum#489 inline loop:

    - **No ``NoneType`` crash (AC4).** A stale/corrupt collection can return a
      ``None`` metadata entry (or a ``None`` ``metadatas`` list); every access
      is guarded so ``'NoneType' object has no attribute 'get'`` can never
      reach the caller — a missing metadata degrades to an empty dict.
    - **Degenerate result sets surface explicitly (AC3).** When two or more
      neighbours come back at an identical distance, the index is not ranking;
      raise :class:`DegradedIndexError` instead of returning flat-scored,
      confidently-wrong hits.

    ``type_narrowed`` (issue athenaeum#964): ``True`` when the caller applied a
    ``where`` type predicate that can legitimately shrink the candidate set to
    a handful of rows (a rare class, e.g. 2 live pages). A tiny, genuinely
    ranked result set commonly ties at the same rounded distance by
    coincidence, not because the index is degraded — the AC3 guard above was
    built to catch a *global, unfiltered* query returning uniformly flat
    scores, not a legitimately narrow one. Skips the guard in that case only;
    an unfiltered (or exclude-only) query is checked exactly as before.
    """
    ids_rows = results.get("ids") or []
    if not ids_rows or not ids_rows[0]:
        return []
    id_row = ids_rows[0]
    meta_rows = results.get("metadatas") or []
    meta_row = meta_rows[0] if meta_rows else []
    dist_rows = results.get("distances") or []
    dist_row = dist_rows[0] if dist_rows else []

    distances = [dist_row[i] if i < len(dist_row) else 0.0 for i in range(len(id_row))]
    if (
        not type_narrowed
        and len(distances) >= 2
        and len({round(d, 6) for d in distances}) == 1
    ):
        raise DegradedIndexError(
            f"vector index returned {len(distances)} results at an identical "
            f"distance ({distances[0]}); this is a degraded/unavailable index, "
            "not a ranking. Rebuild it with `athenaeum reindex`."
        )

    hits: list[tuple[str, str, float]] = []
    for i, doc_id in enumerate(id_row):
        meta = meta_row[i] if i < len(meta_row) else None
        if not isinstance(meta, dict):
            meta = {}
        if caller_audience is not None:
            audience_str = str(meta.get("audience", "|"))
            if not audience_string_authorized(audience_str, caller_audience):
                continue
        name = meta.get("name", doc_id.replace(".md", ""))
        hits.append((doc_id, name, distances[i]))
        if len(hits) >= n:
            break
    return hits


class VectorBackend:
    """Semantic search via chromadb with local embeddings.

    Requires ``pip install athenaeum[vector]`` (chromadb).
    Uses the default ``all-MiniLM-L6-v2`` embedding model unless an
    alternate model name is passed (issue athenaeum#315 config seam).
    """

    # Text length used both as the embedded document and as the batch cap.
    _DOC_LIMIT = 4000
    _BATCH_SIZE = 5000

    # Issue athenaeum#964 (AC amendment 1): the filterable-METADATA contract
    # version, stamped into the manifest at build time and compared before an
    # incremental build the same way ``FTS5Backend._SCHEMA_VERSION`` gates the
    # FTS5 table shape. Bump whenever a metadata key is added/removed/
    # reinterpreted. A mismatch (including a pre-athenaeum#964 manifest, which has
    # no key at all) forces a FULL rebuild — the athenaeum#370 stat pre-filter would
    # otherwise leave every untouched page's metadata on the old shape
    # forever. Version 2 == the ``type``-aware shape.
    _METADATA_SCHEMA_VERSION = 2

    def __init__(self, embedding_model: str | None = None) -> None:
        """Construct the backend.

        ``embedding_model`` (issue athenaeum#315 seam) selects the sentence-transformer
        model. ``None`` and the documented default ``all-MiniLM-L6-v2`` both
        use chromadb's built-in default embedding function unchanged — the
        default is NOT changed here. A non-default name is only honored if
        chromadb's sentence-transformer EF can load it; the manifest records
        the model so swapping it forces a one-time full re-embed (the eval
        opportunity noted at DEFAULT_EMBEDDING_MODEL).
        """
        self.embedding_model = embedding_model or DEFAULT_EMBEDDING_MODEL
        # Build-generation this process last opened a client for (issue athenaeum#489).
        # ``None`` forces a cache-clear on the first query so a process that
        # started before an out-of-process reindex never serves stale results.
        self._seen_generation: str | None = None

    def _refresh_on_reindex(self, vector_dir: Path) -> None:
        """Clear chromadb's process-global cache if the index was rebuilt (athenaeum#489).

        chromadb caches ``PersistentClient`` *systems* per-path at the module
        level (``SharedSystemClient``). An out-of-process ``athenaeum reindex``
        replaces the on-disk collection but leaves THIS process's cached system
        pinned to the old (now-deleted) state — so a subsequent open serves
        silently-degraded results, then hard-crashes with ``'NoneType' object
        has no attribute 'get'``, until the process is restarted.

        Compare the on-disk build-generation token against what we last opened;
        on any change, drop the cached system so the next ``PersistentClient``
        re-reads the true on-disk state. This is the same reset ``build_index``
        performs in the *reindexing* process — done here for the *reading*
        (server) process, which never sees that reindex.
        """
        generation = _read_generation(vector_dir)
        if generation == self._seen_generation:
            return
        try:
            from chromadb.api.client import SharedSystemClient

            SharedSystemClient.clear_system_cache()
        except Exception:  # noqa: BLE001 — pragma: no cover - chromadb internals moved
            # If the internal moved, we simply don't get auto-reopen — the
            # pre-athenaeum#489 behaviour — never a crash from the fix itself.
            pass
        self._seen_generation = generation

    def _get_chromadb(self) -> Any:
        try:
            import chromadb

            return chromadb
        except ImportError as exc:
            raise ImportError(
                "Vector backend requires chromadb. "
                "Install with: pip install athenaeum[vector]"
            ) from exc

    def _embedding_function(self) -> Any | None:
        """Return the chromadb embedding function, or ``None`` for the default.

        The default model uses chromadb's built-in EF (``None``) so behavior
        is byte-for-byte unchanged. A non-default model constructs a
        SentenceTransformer EF; if that import/construction fails we fall
        back to the default rather than crashing the rebuild.
        """
        if self.embedding_model == DEFAULT_EMBEDDING_MODEL:
            return None
        try:
            from chromadb.utils import embedding_functions

            return embedding_functions.SentenceTransformerEmbeddingFunction(
                model_name=self.embedding_model
            )
        except Exception:  # noqa: BLE001 — pragma: no cover - optional-model fallback
            import logging

            logging.getLogger(__name__).warning(
                "embedding model %r unavailable; falling back to default %r",
                self.embedding_model,
                DEFAULT_EMBEDDING_MODEL,
            )
            return None

    def _add_records(
        self,
        collection: Any,
        records: list[tuple[str, Path, str, str, dict[str, Any], tuple[str, str]]],
    ) -> None:
        """Embed and add a batch of scanned records to the collection."""
        ids: list[str] = []
        documents: list[str] = []
        metadatas: list[dict[str, str]] = []
        for indexed_name, path, _h, text, meta, _s in records:
            name, _tags, _aliases, _description = _extract_frontmatter_fields(text)
            if not name:
                name = path.stem
            # Issue athenaeum#312 — Layer A: store the effective audience so the query
            # can pre-filter neighbors. chromadb metadata is scalar-only, so
            # the audience is stored as the same delimited string as FTS5 and
            # filtered in Python at query time (Layer B).
            ids.append(indexed_name)
            # Issue athenaeum#426 (stub hygiene): a pointer stub contributes NOTHING
            # beyond its one-line pointer body to embeddings — embedding the
            # full frontmatter+body (like every other page) would defeat the
            # point of converting a duplicate into a stub in the first place.
            # Fall back to the full ``text`` when the page has no frontmatter
            # (parse_frontmatter returned an empty dict) so a non-wiki-shaped
            # file is unaffected.
            if meta and is_pointer_stub(meta):
                _fm, doc_body = parse_frontmatter(text)
                doc_text = doc_body.strip()
            else:
                doc_text = text
            documents.append(doc_text[: self._DOC_LIMIT])
            metadatas.append(
                {
                    "name": name,
                    "filename": indexed_name,
                    "audience": audience_index_string(meta),
                    # Issue athenaeum#964: same precedence resolver the FTS5 ``type``
                    # column uses (top-level ``type:`` wins, ``metadata.type``
                    # falls back), so a page found by one backend's filter is
                    # found by the other's too.
                    "type": resolve_page_type(meta),
                }
            )
        for i in range(0, len(ids), self._BATCH_SIZE):
            end = min(i + self._BATCH_SIZE, len(ids))
            collection.add(
                ids=ids[i:end],
                documents=documents[i:end],
                metadatas=metadatas[i:end],
            )

    def build_index(
        self,
        wiki_root: Path,
        cache_dir: Path,
        *,
        extra_roots: Iterable[Path] | None = None,
        incremental: bool = True,
        include_globs: Iterable[str] | None = None,
        exclude_globs: Iterable[str] | None = None,
        as_of: date | None = None,
        full_rehash_max_age_days: float = _DEFAULT_FULL_REHASH_MAX_AGE_DAYS,
        config: dict[str, Any] | None = None,
    ) -> int:
        """Build a chromadb collection from wiki + extra intake roots.

        See :meth:`SearchBackend.build_index` for the full contract.
        Incremental by default (issue athenaeum#348): only added/changed/removed
        pages are (re-)embedded, keyed off a whole-file content-hash
        manifest sidecar. A no-op rebuild re-embeds nothing. An as-of build
        (``as_of`` set, issue athenaeum#308) is always a full build reflecting that
        date's validity windows. ``full_rehash_max_age_days`` (issue athenaeum#373)
        periodically forces a full re-hash on the incremental path — the change
        delta is still applied incrementally (no full re-embed).
        """
        chromadb = self._get_chromadb()
        cache_dir.mkdir(parents=True, exist_ok=True)
        vector_dir = cache_dir / _VECTOR_DIR
        manifest_path = cache_dir / _VECTOR_MANIFEST

        # Issue athenaeum#308: an as-of view is a historical snapshot — never diff it
        # against (or seed) the live manifest, so force a full build.
        stored = (
            _load_manifest(manifest_path) if incremental and as_of is None else None
        )
        stored_model = stored.get("embedding_model") if stored else None
        # Issue athenaeum#964 (AC amendment 1): a manifest predating the metadata
        # schema stamp (``None``) or stamped with an older contract version
        # must NOT be reused incrementally — same rule ``stored_model`` above
        # already applies for an embedding-model swap.
        stored_metadata_schema = stored.get("metadata_schema_version") if stored else None
        # Incremental only when we have a prior manifest, a live collection
        # dir, the SAME embedding model (a model swap must re-embed all), AND
        # the SAME metadata schema version.
        do_incremental = (
            incremental
            and as_of is None
            and stored is not None
            and vector_dir.is_dir()
            and stored_model == self.embedding_model
            and stored_metadata_schema == self._METADATA_SCHEMA_VERSION
        )

        # Issue athenaeum#373: self-healing full-re-hash backstop (identical to FTS5).
        # On the incremental path, force a full re-hash of every file when the
        # manifest has not recorded one within the max age — the change delta is
        # still applied incrementally (no rmtree / full re-embed).
        #
        # Issue athenaeum#977: also stale when the manifest predates the opaque-token
        # schema — see the identical comment in ``FTS5Backend.build_index``.
        now = _now()
        last_rehash = _manifest_last_full_rehash(stored)
        schema_mismatch = (
            stored is not None and stored.get("version") != _STORE_STATS_SCHEMA_VERSION
        )
        stale = (
            last_rehash is None
            or schema_mismatch
            or (now - last_rehash) > (full_rehash_max_age_days * 86400.0)
        )

        # Issue athenaeum#370: stat pre-filter the scan on the incremental path only —
        # a full (re)build embeds every scanned record and cannot use the
        # placeholder text/meta that stat-matched rows carry. A stale incremental
        # build (athenaeum#373) also passes ``prior=None`` to force a re-hash of all.
        prior = _scan_prior(stored) if (do_incremental and not stale) else None

        def _scan(with_prior: dict[str, tuple[str, str, str]] | None) -> tuple[
            list[tuple[str, Path, str, str, dict[str, Any], tuple[str, str]]],
            dict[str, str],
            dict[str, tuple[str, str]],
        ]:
            recs = list(
                _scan_indexed_records(
                    wiki_root,
                    extra_roots,
                    include_globs=include_globs,
                    exclude_globs=exclude_globs,
                    as_of=as_of,
                    prior=with_prior,
                    config=config,
                )
            )
            return (
                recs,
                {name: h for name, _p, h, _t, _m, _s in recs},
                {name: s for name, _p, _h, _t, _m, s in recs},
            )

        current, current_hashes, current_stats = _scan(prior)

        # chromadb caches PersistentClient systems per-path at the module
        # level. Clear it so a fresh client sees the true on-disk state
        # (avoids stale-collection "already exists" desync — see issue athenaeum#32).
        from chromadb.api.client import SharedSystemClient

        SharedSystemClient.clear_system_cache()

        if do_incremental:
            try:
                client = chromadb.PersistentClient(path=str(vector_dir))
                collection = client.get_collection(
                    _VECTOR_COLLECTION,
                    embedding_function=self._embedding_function(),
                )
            except Exception as exc:  # noqa: BLE001 — corrupt/missing collection: fall back to full rebuild
                # Corrupt / missing collection despite a manifest — fall back
                # to a clean full rebuild rather than accreting a bad delta.
                # Issue athenaeum#370: log it — a silent full rmtree+re-embed of a 21k
                # corpus was indistinguishable from a hang. WARNING so a real
                # (expensive) full rebuild is diagnosable, not silent.
                import logging

                logging.getLogger(__name__).warning(
                    "vector incremental open failed (%s: %s); "
                    "falling back to FULL rebuild (rmtree + re-embed all)",
                    type(exc).__name__,
                    exc,
                )
                do_incremental = False

        if not do_incremental:
            # A stat pre-filtered scan yields placeholder bodies for unchanged
            # rows; a full rebuild embeds every row, so re-scan with full reads
            # first (only when we took the fast-path).
            if prior is not None:
                current, current_hashes, current_stats = _scan(None)
            # Full (re)build — nuke any prior on-disk state before opening a
            # PersistentClient. chromadb's SQLite metadata and the rust
            # binding's collection store can desync; a full wipe is the
            # simplest robust reset (issue athenaeum#32).
            if vector_dir.exists():
                shutil.rmtree(vector_dir)
            vector_dir.mkdir(parents=True, exist_ok=True)
            SharedSystemClient.clear_system_cache()
            client = chromadb.PersistentClient(path=str(vector_dir))
            collection = client.create_collection(
                _VECTOR_COLLECTION,
                embedding_function=self._embedding_function(),
            )
            self._add_records(collection, current)
            _write_manifest(
                manifest_path,
                current_hashes,
                {
                    "embedding_model": self.embedding_model,
                    "metadata_schema_version": self._METADATA_SCHEMA_VERSION,
                },
                stats=current_stats,
                last_full_rehash_at=now,
            )
            _write_generation(vector_dir)  # athenaeum#489: mark this rebuild for readers
            return len(current)

        # Incremental path — diff and apply only the delta.
        stored_hashes = _manifest_hashes(stored)
        added, changed, removed = _compute_delta(current_hashes, stored_hashes)

        to_delete = removed + changed
        if to_delete:
            collection.delete(ids=to_delete)
        reindex = set(added) | set(changed)
        if reindex:
            self._add_records(
                collection,
                [rec for rec in current if rec[0] in reindex],
            )
        total = int(collection.count())
        _write_manifest(
            manifest_path,
            current_hashes,
            {
                "embedding_model": self.embedding_model,
                "metadata_schema_version": self._METADATA_SCHEMA_VERSION,
            },
            stats=current_stats,
            last_full_rehash_at=(now if stale else last_rehash),
        )
        _write_generation(vector_dir)  # athenaeum#489: mark this rebuild for readers
        return total

    def query(
        self,
        query: str,
        cache_dir: Path,
        *,
        n: int = 5,
        exclude: set[str] | None = None,
        wiki_root: Path | None = None,
        caller_audience: set[str] | None = None,
        as_of: date | None = None,
        type_filter: str | Sequence[str] | None = None,
    ) -> list[tuple[str, str, float]]:
        """Query the chromadb collection with semantic search."""
        del wiki_root  # Vector reads the pre-built chromadb collection
        del as_of  # athenaeum#308: vector filters at build time; as-of view = as-of index
        chromadb = self._get_chromadb()

        vector_dir = cache_dir / _VECTOR_DIR
        if not vector_dir.is_dir():
            return []

        # athenaeum#489: re-open if an out-of-process reindex replaced the collection.
        self._refresh_on_reindex(vector_dir)

        client = chromadb.PersistentClient(path=str(vector_dir))
        try:
            collection = client.get_collection(_VECTOR_COLLECTION)
        except Exception as exc:  # noqa: BLE001 — chromadb's exception class moves across releases; can't import it directly
            # chromadb raises an InvalidCollectionException (and occasionally
            # bare ValueError from the rust binding) when the collection is
            # absent or its metadata is corrupt. We can't import the exception
            # class directly because chromadb reorganises it between releases,
            # so we catch broadly but log the class name so a real bug
            # doesn't sit silent — "vector returns nothing" was the top
            # first-adopter confusion in the v0.2.0 review.
            import logging

            logging.getLogger(__name__).warning(
                "vector get_collection(%s) failed with %s: %s; " "returning empty hits",
                _VECTOR_COLLECTION,
                type(exc).__name__,
                exc,
            )
            return []

        count = collection.count()
        if count == 0:
            return []

        # Build where filter for exclusions
        exclude_clause: dict[str, Any] | None = None
        if exclude and len(exclude) == 1:
            exclude_clause = {"filename": {"$ne": next(iter(exclude))}}
        elif exclude and len(exclude) > 1:
            exclude_clause = {"filename": {"$nin": list(exclude)}}

        # Issue athenaeum#964: push the type predicate INSIDE the ``where`` — same
        # rule the audience predicate below already follows for chromadb (as
        # far as chromadb's ``where=`` can express it; ``type`` IS a single
        # scalar metadata key, so — unlike ``audience`` — it composes directly
        # as a native ``where`` clause, no Python post-filter needed).
        normalized_types = normalize_type_filter(type_filter)
        type_clause: dict[str, Any] | None = None
        if normalized_types is not None:
            type_clause = (
                {"type": normalized_types[0]}
                if len(normalized_types) == 1
                else {"type": {"$in": list(normalized_types)}}
            )

        # Compose exclude + type WITH ``$and`` rather than one overwriting the
        # other (issue athenaeum#964) — a call passing both must honor both.
        clauses = [c for c in (exclude_clause, type_clause) if c is not None]
        where: dict[str, Any] | None
        if not clauses:
            where = None
        elif len(clauses) == 1:
            where = clauses[0]
        else:
            where = {"$and": clauses}

        # Issue athenaeum#312 — Layer B (vector): chromadb metadata is scalar-only, so
        # there is no native substring/list-membership operator to express the
        # audience predicate as a ``where``. Instead OVER-FETCH — for a
        # restricted caller fetch the full ordered neighbor list — then filter
        # in Python and re-truncate to ``n``. Because we fetch every neighbor,
        # no permitted page can be starved out of the top-k by forbidden
        # neighbors ranking above it. ``caller_audience=None`` (owner) keeps the
        # original cheap ``min(n, count)`` fetch. Layer C (mcp_server) re-checks
        # fresh on-disk frontmatter as the backstop.
        # NOTE (perf): for a restricted caller this is a full-collection kNN
        # (n_results == count) — O(collection) per query — deliberately, so a
        # forbidden-heavy corpus can never starve a permitted page out of the
        # returned top-k. Fine for a personal knowledge base; revisit with a
        # bounded over-fetch + retry only if this ever dominates recall latency.
        fetch_n = count if caller_audience is not None else min(n, count)

        results = collection.query(
            query_texts=[query],
            n_results=fetch_n,
            where=where,
        )

        # athenaeum#489 AC3/AC4: guard None metadata (no 'NoneType'.get crash) and
        # surface a degenerate flat-score result set as an explicit error —
        # unless a type filter (athenaeum#964) legitimately narrowed the candidate
        # set, in which case a coincidental distance tie is not a signal.
        return _hits_from_query_results(
            results, n, caller_audience, type_narrowed=normalized_types is not None
        )

    def fetch_embeddings(
        self,
        ids: Iterable[str],
        cache_dir: Path,
    ) -> dict[str, list[float]]:
        """Return ``{id: embedding_vector}`` for the given indexed filenames.

        Narrow accessor for clustering (issue athenaeum#196). Reuses the collection
        built by :meth:`build_index` — does NOT invoke a second embedding
        provider. Missing ids are silently omitted so callers can cluster
        over the intersection of "requested" and "actually indexed".
        Returns ``{}`` when the collection does not exist or is empty.

        Issue athenaeum#370: this is a pure READ of stored embeddings (``get`` with
        ``include=["embeddings"]`` never embeds), so the collection is opened
        with ``embedding_function=None`` — the default arg is a module-level
        ``DefaultEmbeddingFunction()`` that (in a future chromadb) could pull in
        the ONNX model on this read-only path. Passing ``None`` guarantees the
        embedding backend is never constructed here.
        """
        chromadb = self._get_chromadb()
        vector_dir = cache_dir / _VECTOR_DIR
        if not vector_dir.is_dir():
            return {}

        id_list = list(ids)
        if not id_list:
            return {}

        # athenaeum#489: re-open if an out-of-process reindex replaced the collection.
        self._refresh_on_reindex(vector_dir)

        client = chromadb.PersistentClient(path=str(vector_dir))
        try:
            collection = client.get_collection(
                _VECTOR_COLLECTION, embedding_function=None
            )
        except Exception:  # noqa: BLE001 — missing/corrupt collection: degrade to no embeddings
            return {}

        try:
            result = collection.get(ids=id_list, include=["embeddings"])
        except Exception:  # noqa: BLE001 — chromadb read failure: degrade to no embeddings
            return {}

        out: dict[str, list[float]] = {}
        # chromadb returns embeddings as a numpy array — ``x or []`` raises
        # "truth value ambiguous" on it, so normalize with an explicit None
        # check instead of truthiness (issue athenaeum#370: this read path must work).
        result_ids = result.get("ids")
        if result_ids is None:
            result_ids = []
        embeddings = result.get("embeddings")
        if embeddings is None:
            embeddings = []
        for i, doc_id in enumerate(result_ids):
            if i >= len(embeddings):
                continue
            vec = embeddings[i]
            if vec is None:
                continue
            # chromadb returns numpy arrays in some versions — coerce to list
            out[doc_id] = [float(x) for x in vec]
        return out

    def purge_ids(
        self,
        ids: Iterable[str],
        cache_dir: Path,
    ) -> int:
        """Delete the given indexed filenames from the collection (issue athenaeum#425).

        Embedding hygiene for a fold-into-existing merge: when the resolver
        deletes old-slug wiki files after folding them into a canonical page,
        their stale vectors must not linger and surface as near-duplicate
        recall hits. Mirrors :meth:`fetch_embeddings`'s open pattern (a pure
        mutation of the existing collection — never constructs an embedding
        function, since a delete needs no embedding). Returns the number of
        ids requested that were plausibly present (best-effort — chromadb's
        ``delete`` does not report which ids actually existed); returns 0
        when the collection/vector dir does not exist, chromadb is not
        installed, or the input is empty. Never raises — a purge failure
        must not block the merge's file-level side effects, which have
        already happened by the time this runs.
        """
        try:
            chromadb = self._get_chromadb()
        except ImportError:
            return 0
        vector_dir = cache_dir / _VECTOR_DIR
        if not vector_dir.is_dir():
            return 0

        id_list = list(ids)
        if not id_list:
            return 0

        try:
            client = chromadb.PersistentClient(path=str(vector_dir))
            collection = client.get_collection(
                _VECTOR_COLLECTION, embedding_function=None
            )
            collection.delete(ids=id_list)
        except Exception:  # noqa: BLE001 — purge must never break the merge (see docstring)
            return 0
        return len(id_list)

    def query_neighbors(
        self,
        embedding: Sequence[float],
        cache_dir: Path,
        *,
        k: int = 200,
        exclude_ids: Iterable[str] | None = None,
    ) -> list[tuple[str, float]]:
        """Return ``[(id, distance)]`` for the ``k`` nearest stored neighbors.

        Issue athenaeum#370 (delta compile): a by-VECTOR nearest-neighbor accessor for
        the delta-scoped cluster pass. Unlike :meth:`query` (which embeds a
        query *string*), this queries by an already-resolved embedding vector,
        so the collection is opened with ``embedding_function=None`` — this is
        a pure read that never constructs the ONNX embedder.

        The caller OVER-FETCHES (large ``k``) because chromadb's default HNSW
        space is L2, which only approximates cosine ranking; the delta closure
        re-confirms every returned candidate with an exact cosine check before
        treating it as a true single-linkage edge, so ANN ranking noise cannot
        introduce a spurious edge. Returns fewer than ``k`` when the collection
        is smaller. ``exclude_ids`` drops the query file itself (and any known
        non-candidates) via a chromadb ``where`` filter. Returns ``[]`` when the
        collection does not exist or is empty.
        """
        chromadb = self._get_chromadb()
        vector_dir = cache_dir / _VECTOR_DIR
        if not vector_dir.is_dir():
            return []

        client = chromadb.PersistentClient(path=str(vector_dir))
        try:
            collection = client.get_collection(
                _VECTOR_COLLECTION, embedding_function=None
            )
        except Exception:  # noqa: BLE001 — missing/corrupt collection: no neighbors
            return []

        count = collection.count()
        if count == 0:
            return []

        where: dict[str, Any] | None = None
        excluded = [e for e in (exclude_ids or [])]
        if len(excluded) == 1:
            where = {"filename": {"$ne": excluded[0]}}
        elif len(excluded) > 1:
            where = {"filename": {"$nin": excluded}}

        try:
            results = collection.query(
                query_embeddings=[list(embedding)],
                n_results=min(k, count),
                where=where,
            )
        except Exception:  # noqa: BLE001 — chromadb query failure: no neighbors
            return []

        out: list[tuple[str, float]] = []
        ids = results.get("ids") or []
        if ids and ids[0]:
            distances = results.get("distances") or [[]]
            dist_row = distances[0] if distances else []
            for i, doc_id in enumerate(ids[0]):
                dist = float(dist_row[i]) if i < len(dist_row) else 0.0
                out.append((doc_id, dist))
        return out


# ---------------------------------------------------------------------------
# Keyword backend (in-memory, scan-on-query)
# ---------------------------------------------------------------------------


def tokenize_keyword_query(query: str) -> list[str]:
    """Split a query into lowercase tokens of length >=2."""
    return [t for t in re.split(r"\W+", query.lower()) if len(t) >= 2]


def score_keyword_page(tokens: list[str], frontmatter: dict, body: str) -> float:
    """Score a wiki page against keyword tokens.

    Frontmatter fields (``name``, ``aliases``, ``tags``, ``description``,
    ``title``) match at 3x the weight of body hits.
    """
    if not tokens:
        return 0.0

    fm_parts: list[str] = []
    for key in ("name", "aliases", "tags", "description", "title"):
        val = frontmatter.get(key, "")
        if isinstance(val, list):
            fm_parts.append(" ".join(str(v) for v in val))
        else:
            fm_parts.append(str(val))
    fm_text = " ".join(fm_parts).lower()
    body_lower = body.lower()

    score = 0.0
    for token in tokens:
        score += fm_text.count(token) * 3.0
        score += body_lower.count(token) * 1.0
    return score


class KeywordScanNotSupportedError(RuntimeError):
    """Raised when :class:`KeywordBackend` is queried against a surface that
    does not declare ``cheap_local_scan`` (design note §7 honest-refusal
    rule; issue athenaeum#981, slice S6 of the whole-store adapter design
    lock, athenaeum#911).

    ``KeywordBackend.query`` re-globs and re-reads every page on **every**
    query (design note §3.1 walk W2) — cheap only because a local ``stat`` +
    ``read`` is effectively free. Unlike an index *build* (where
    incrementality amortises the cost, §3.3), the scan-on-query walk IS the
    query, so no amount of incrementality helps on a surface where each
    operation carries real latency: it would become an unbounded
    full-corpus round-trip on every single call. Per R4 ("no silent
    degradation"), the honest response is to refuse and name what is
    missing rather than pay that cost quietly.
    """


class KeywordBackend:
    """Scan-on-query keyword scoring over wiki frontmatter + body.

    No pre-built index: every query rereads the wiki. Frontmatter hits
    are weighted 3x body hits. Intended as a zero-setup fallback for
    small wikis or tests — FTS5 is the recommended default for real use.
    """

    def build_index(
        self,
        wiki_root: Path,
        cache_dir: Path,
        *,
        extra_roots: Iterable[Path] | None = None,
        incremental: bool = True,
        include_globs: Iterable[str] | None = None,
        exclude_globs: Iterable[str] | None = None,
        as_of: date | None = None,
        full_rehash_max_age_days: float = _DEFAULT_FULL_REHASH_MAX_AGE_DAYS,
        config: dict[str, Any] | None = None,
    ) -> int:
        """No-op: the keyword backend rescans on every query.

        Returns a count that includes wiki entries + extra-root entries
        (``MEMORY.md`` and non-``.md`` files excluded) so status checks
        see a comparable number to the indexed backends. The ``incremental``
        / glob knobs (issue athenaeum#348), ``as_of`` (issue athenaeum#308), and
        ``full_rehash_max_age_days`` (issue athenaeum#373) are accepted for Protocol
        parity but inert here — there is no persisted manifest to diff, scope,
        or re-hash, and the temporal filter is applied at QUERY time
        (:meth:`query`), not here.
        """
        del cache_dir, incremental, include_globs, exclude_globs, as_of
        del full_rehash_max_age_days
        # Issue athenaeum#532: the storage-adapter ``embedded`` policy is a persisted-index
        # concept; the keyword backend has no persisted index, so it is inert
        # here. Recall-time enforcement of the ``recallable`` policy applies to
        # keyword results the same as every backend, at the recall render layer
        # (``mcp_server._recall_via_backend``).
        del config
        # Issue athenaeum#898: directory-segment-aware exclusion (not just
        # p.name) — see _wiki_relpath_excluded's docstring for why a
        # filename-only check missed wiki/_quarantine/... entirely.
        count = sum(
            1
            for p in wiki_root.rglob("*.md")
            if not _wiki_relpath_excluded(p.relative_to(wiki_root))
            and p.name not in _INTAKE_SKIP_NAMES
        )
        if extra_roots:
            for root in extra_roots:
                if not root.is_dir():
                    continue
                count += sum(
                    1 for p in root.rglob("*.md") if p.name not in _INTAKE_SKIP_NAMES
                )
        return count

    def query(
        self,
        query: str,
        cache_dir: Path,
        *,
        n: int = 5,
        exclude: set[str] | None = None,
        wiki_root: Path | None = None,
        caller_audience: set[str] | None = None,
        as_of: date | None = None,
        type_filter: str | Sequence[str] | None = None,
        store: Store | None = None,
    ) -> list[tuple[str, str, float]]:
        """Score every non-underscore wiki page and return the top-n hits.

        ``as_of`` (issue athenaeum#308 slice 3) filters each page against its validity
        window at query time — the keyword backend scans on query, so it honors
        an as-of *rewind* directly (no as-of index build needed). ``None`` =
        today.

        ``store`` (issue athenaeum#981, design note §7 honest-refusal rule): the
        capability gate for this backend's scan-on-query walk (§3.1 W2), which
        re-globs and re-reads every page on every call — cheap only on a
        surface with ``cheap_local_scan``. Injectable so a test can supply a
        fake declaring ``capabilities.cheap_local_scan=False`` without a real
        non-filesystem adapter. Defaults to a
        :class:`~athenaeum.store.FilesystemStore` over *wiki_root*, which
        always declares ``cheap_local_scan=True`` — so the zero-setup
        filesystem fallback this backend exists for is unaffected.
        """
        del cache_dir
        if wiki_root is None or not wiki_root.is_dir():
            return []

        store = store if store is not None else FilesystemStore(wiki_root, {})
        if not store.capabilities.cheap_local_scan:
            raise KeywordScanNotSupportedError(
                f"KeywordBackend cannot query {wiki_root}: this surface does "
                "not declare cheap_local_scan, so a full-corpus scan on every "
                "query (design note §3.1 W2) would be an unbounded round-trip "
                "rather than a cheap local re-read. Use the FTS5 backend "
                "instead — it builds a persisted index rather than "
                "rescanning the store on every query."
            )

        tokens = tokenize_keyword_query(query)
        if not tokens:
            return []

        # Issue athenaeum#964: the keyword backend has no persisted index (it scans
        # frontmatter fresh every query), so honoring the type filter needs no
        # index change at all — just an extra predicate on the same
        # already-parsed ``fm`` dict, checked BEFORE scoring so a non-matching
        # page never enters ``scored`` and cannot occupy a top-n slot (same
        # "pushed inside the query" rule as the audience check below, not a
        # post-filter over the sorted/truncated result list).
        normalized_types = normalize_type_filter(type_filter)

        excluded = exclude or set()
        scored: list[tuple[float, str, str]] = []
        for md_file in wiki_root.rglob("*.md"):
            try:
                rel_path = md_file.relative_to(wiki_root)
            except ValueError:
                rel_path = Path(md_file.name)
            # Issue athenaeum#898: directory-segment-aware exclusion (not just
            # md_file.name) — see _wiki_relpath_excluded's docstring for why
            # a filename-only check missed wiki/_quarantine/... entirely.
            if _wiki_relpath_excluded(rel_path):
                continue
            rel = rel_path.as_posix()
            if rel in excluded:
                continue
            try:
                text = md_file.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue

            fm, body = parse_frontmatter(text)
            # Issue athenaeum#191: skip inactive members (superseded_by / deprecated).
            # Issue athenaeum#308 slice 3: also skip pages outside their validity window
            # relative to ``as_of`` (default today) — the query-time as-of view.
            # Issue athenaeum#904: ``_is_recall_inactive`` lets an expired
            # ``bucket: daily`` page stay recall-visible for currency ranking
            # instead of being hard-dropped here.
            if _is_recall_inactive(fm, as_of):
                continue
            # Issue athenaeum#427: belt-and-suspenders — a ``pii: true``-flagged page is
            # excluded from keyword recall too, even though this backend scans
            # on query rather than a pre-built index.
            if is_pii_flagged(fm):
                continue
            # Issue athenaeum#312 — Layer B (keyword): authorize BEFORE scoring so a
            # forbidden page never enters ``scored`` and cannot occupy a top-n
            # slot. Owner (caller_audience=None) is authorized for everything.
            if not is_page_authorized(fm, caller_audience):
                continue
            if normalized_types is not None and resolve_page_type(fm) not in normalized_types:
                continue
            score = score_keyword_page(tokens, fm, body)
            if score > 0:
                name = fm.get("name") or md_file.stem
                scored.append((score, rel, str(name)))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [(fname, name, score) for score, fname, name in scored[:n]]


# ---------------------------------------------------------------------------
# Backend registry
# ---------------------------------------------------------------------------

_BACKENDS: dict[str, type[SearchBackend]] = {
    "fts5": FTS5Backend,
    "vector": VectorBackend,
    "keyword": KeywordBackend,
}


def get_backend(name: str, **kwargs: Any) -> SearchBackend:
    """Return a backend instance by name. Raises ``KeyError`` for unknown names.

    ``kwargs`` are forwarded to the backend's constructor (issue athenaeum#542) — e.g.
    ``get_backend("vector", embedding_model="...")`` — so callers that need a
    non-default constructor arg go through the registry instead of
    instantiating a concrete backend class directly.
    """
    cls = _BACKENDS.get(name)
    if cls is None:
        raise KeyError(
            f"Unknown search backend {name!r}. "
            f"Available: {', '.join(sorted(_BACKENDS))}"
        )
    return cls(**kwargs)


# ---------------------------------------------------------------------------
# Convenience functions for shell hook scripts
# ---------------------------------------------------------------------------


def _coerce_as_of(as_of: date | str | None) -> date | None:
    """Coerce an ``as_of`` argument (ISO string or ``date``) to a ``date``.

    Convenience for shell-hook / CLI callers that pass ``as_of`` as an
    ISO-8601 ``YYYY-MM-DD`` string. ``None`` and ``date`` pass through. A
    non-empty unparseable string raises ``ValueError`` — unlike the fail-open
    frontmatter parse, an operator explicitly asking for an as-of view with a
    bad date should get a loud error, not a silent today-view.
    """
    if as_of is None or isinstance(as_of, date):
        return as_of
    return date.fromisoformat(as_of.strip())


def build_fts5_index(
    wiki_root: str | Path,
    cache_dir: str | Path,
    *,
    extra_roots: Iterable[str | Path] | None = None,
    incremental: bool = True,
    include_globs: Iterable[str] | None = None,
    exclude_globs: Iterable[str] | None = None,
    as_of: date | str | None = None,
    full_rehash_max_age_days: float = _DEFAULT_FULL_REHASH_MAX_AGE_DAYS,
    config: dict[str, Any] | None = None,
) -> int:
    """Build an FTS5 index. Callable from shell hooks via ``python3 -c``.

    ``extra_roots`` accepts the same list as
    :meth:`FTS5Backend.build_index` (additional intake directories
    scanned recursively, e.g. ``~/knowledge/raw/auto-memory``).
    ``incremental`` (default ``True``, issue athenaeum#348) applies only the
    add/change/delete delta; pass ``False`` to force a full rebuild.

    ``as_of`` (issue athenaeum#308) builds an as-of *rewind* index: pass an ISO date
    string or a ``date`` to reflect the knowledge base as it stood then
    (always a full build). ``None`` (default) means today.
    ``full_rehash_max_age_days`` (issue athenaeum#373) sets the periodic full-re-hash
    backstop for the stat pre-filter.
    """
    roots = [Path(r) for r in extra_roots] if extra_roots else None
    return FTS5Backend().build_index(
        Path(wiki_root),
        Path(cache_dir),
        extra_roots=roots,
        incremental=incremental,
        include_globs=include_globs,
        exclude_globs=exclude_globs,
        as_of=_coerce_as_of(as_of),
        full_rehash_max_age_days=full_rehash_max_age_days,
        config=config,
    )


def query_fts5_index(
    query: str,
    cache_dir: str | Path,
    *,
    n: int = 3,
    exclude: set[str] | None = None,
) -> list[tuple[str, str, float]]:
    """Query the FTS5 index. Callable from shell hooks via ``python3 -c``."""
    return FTS5Backend().query(query, Path(cache_dir), n=n, exclude=exclude)


def build_vector_index(
    wiki_root: str | Path,
    cache_dir: str | Path,
    *,
    extra_roots: Iterable[str | Path] | None = None,
    incremental: bool = True,
    include_globs: Iterable[str] | None = None,
    exclude_globs: Iterable[str] | None = None,
    embedding_model: str | None = None,
    as_of: date | str | None = None,
    full_rehash_max_age_days: float = _DEFAULT_FULL_REHASH_MAX_AGE_DAYS,
    config: dict[str, Any] | None = None,
) -> int:
    """Build a chromadb vector index. Callable from shell hooks.

    ``extra_roots`` accepts the same list as
    :meth:`VectorBackend.build_index`. ``incremental`` (default ``True``,
    issue athenaeum#348) re-embeds only the delta. ``embedding_model`` (issue athenaeum#315
    seam) defaults to ``all-MiniLM-L6-v2`` — the documented default is not
    changed here; swapping it forces a one-time full re-embed. ``as_of``
    (issue athenaeum#308) builds an as-of *rewind* index — see :func:`build_fts5_index`.
    ``full_rehash_max_age_days`` (issue athenaeum#373) sets the periodic full-re-hash
    backstop for the stat pre-filter.
    """
    roots = [Path(r) for r in extra_roots] if extra_roots else None
    return get_backend("vector", embedding_model=embedding_model).build_index(
        Path(wiki_root),
        Path(cache_dir),
        extra_roots=roots,
        incremental=incremental,
        include_globs=include_globs,
        exclude_globs=exclude_globs,
        as_of=_coerce_as_of(as_of),
        full_rehash_max_age_days=full_rehash_max_age_days,
        config=config,
    )


def query_vector_index(
    query: str,
    cache_dir: str | Path,
    *,
    n: int = 3,
    exclude: set[str] | None = None,
) -> list[tuple[str, str, float]]:
    """Query the chromadb vector index. Callable from shell hooks."""
    return get_backend("vector").query(query, Path(cache_dir), n=n, exclude=exclude)


# ---------------------------------------------------------------------------
# Embedding helpers (issue athenaeum#211 — decision-log semantic matching)
# ---------------------------------------------------------------------------

# Module-level memoized chromadb embedding function instance.  Loaded lazily
# so the module can be imported when chromadb is absent (it is an optional
# ``[vector]`` dependency).  When chromadb is not installed this stays ``None``
# and all callers gracefully degrade.
_EF: Any | None = None
_EF_LOADED: bool = False  # True once we have tried to load (even if None)

# Issue athenaeum#1032: one-time WARNING flag for ``embed_texts`` returning ``None``.
# Separate from ``_EF_LOADED`` above — ``_get_ef`` only fails once (init is
# memoized), but ``embed_texts`` can also return ``None`` on a per-call embedding
# failure (``ef`` initialized fine, the call itself raised) even after ``_get_ef``
# has already succeeded once, so it needs its own one-time guard.
_EMBED_TEXTS_NONE_WARNED: bool = False


def _get_ef() -> Any | None:
    """Return a memoized chromadb DefaultEmbeddingFunction, or None."""
    global _EF, _EF_LOADED
    if _EF_LOADED:
        return _EF
    _EF_LOADED = True
    try:
        from chromadb.utils import embedding_functions

        _EF = embedding_functions.DefaultEmbeddingFunction()
    except Exception as exc:  # noqa: BLE001 — ImportError, any chromadb init error: degrade to None
        _EF = None
        # Issue athenaeum#1032: one-time WARNING (the ``_EF_LOADED`` memo above already
        # guarantees this branch runs at most once per process) naming the
        # exception class/message — the coarse embedding-fallback path used to
        # degrade completely silently, making athenaeum#1005's over-cluster diagnosis
        # unfalsifiable from run artifacts.
        import logging

        logging.getLogger(__name__).warning(
            "search: chromadb embedding function failed to initialize (%s: %s) — "
            "embed_texts will return None for the rest of this process; callers "
            "needing vectors (e.g. wiki-page dedup clustering) fall back to the "
            "fallback-hashing embedder to produce them (issue athenaeum#1032)",
            type(exc).__name__,
            exc,
        )
    return _EF


def embed_texts(texts: list[str]) -> list[list[float]] | None:
    """Embed a list of texts using chromadb's default EF.

    Returns a list of float vectors (one per input string), or ``None`` when
    chromadb is not installed or the embedding call fails.  This function is
    the injectable default used by :func:`athenaeum.fingerprint.find_resolved_record`
    for the embedding similarity strategy.  Tests MUST inject a stub embedder —
    never rely on real chromadb in the test suite.
    """
    ef = _get_ef()
    if ef is None:
        _warn_embed_texts_none_once()
        return None
    try:
        result = ef(texts)
        # chromadb EF returns a list-like of list-likes; normalise to list[list[float]]
        return [list(map(float, vec)) for vec in result]
    except Exception as exc:  # noqa: BLE001 — embedding call failure: degrade to None (caller falls back)
        _warn_embed_texts_none_once(exc)
        return None


def _warn_embed_texts_none_once(exc: BaseException | None = None) -> None:
    """One-time WARNING (issue athenaeum#1032) when ``embed_texts`` returns ``None``.

    Fires at most once per process regardless of which of the two ``None``
    paths above triggered it (no ``ef`` available vs. the embedding call
    itself raising) — matching the memoized-failure shape ``_get_ef`` already
    uses, so a hot loop of failing calls does not spam the log.
    """
    global _EMBED_TEXTS_NONE_WARNED
    if _EMBED_TEXTS_NONE_WARNED:
        return
    _EMBED_TEXTS_NONE_WARNED = True
    import logging

    if exc is None:
        logging.getLogger(__name__).warning(
            "search: embed_texts has no embedding function available; callers "
            "needing vectors (e.g. wiki-page dedup clustering) fall back to the "
            "fallback-hashing embedder to produce them (issue athenaeum#1032)"
        )
    else:
        logging.getLogger(__name__).warning(
            "search: embed_texts call failed (%s: %s); callers needing vectors "
            "(e.g. wiki-page dedup clustering) fall back to the fallback-hashing "
            "embedder to produce them (issue athenaeum#1032)",
            type(exc).__name__,
            exc,
        )
