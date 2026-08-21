# SPDX-License-Identifier: Apache-2.0
"""Authority manifest + duplicate detector + pointer-stub converter (issue athenaeum#426).

Stops memories from duplicating content a **live source** (a skill file, a
code path, a config file) already owns. A live source can drift out from
under a stale memory copy silently; a *pointer* that names the live location
cannot go stale in the same way — recall always resolves to whatever the
source currently says.

This module is the standalone, unit-testable slice: it builds the manifest
format + loader/validator, the lookup-based duplicate detector, and the
pointer-stub converter. It deliberately does NOT wire into any reasoning-tier
consumption path (that is athenaeum#423's T1 duplicate bin / athenaeum#432's T2 rejection) and
does NOT run against the live corpus (that is operator task athenaeum#437) — see the
issue body for the re-scope rationale.

Manifest format + location (design choice, documented here):

- **Format: YAML.** Every other athenaeum config artifact (``athenaeum.yaml``,
  the eval ``cases.yaml`` fixtures) is YAML; a second format would be pure
  inconsistency with no offsetting benefit for a small, human-maintained
  registry.
- **Location:** ``<knowledge_root>/authority-manifest.yaml`` by default —
  a sibling of ``athenaeum.yaml`` at the knowledge-root, resolved via
  :func:`athenaeum.config.resolve_authority_manifest_path` (env >
  ``librarian.authority_manifest_path`` yaml > default), mirroring the
  config-resolution precedence used throughout :mod:`athenaeum.config`.
- **Schema** (top-level):

  .. code-block:: yaml

      version: 1
      sources:
        - slug: skill-dijkstra           # unique id; referenced by stubs
          location: .claude/skills/dijkstra/SKILL.md
          kind: skill                    # skill | code | config | doc
          topics:                        # slugs/topics this source OWNS
            - lean-development-workflow
            - clean-commit-discipline
      never_ingest_classes:              # optional (issue athenaeum#968); empty/absent
        - mirror-of-live-source          # by default -- no new intake refusal
        - pending-state-todo             # until an operator opts a class in

  ``version`` must be the literal integer ``1`` (schema-evolution seam — a
  future incompatible schema bumps it and the loader can dispatch on it).
  Each source requires ``slug`` (unique, non-empty), ``location``
  (non-empty — where the live source lives), and ``topics`` (non-empty list
  of non-empty strings). ``kind`` is optional free text (not validated
  against a closed vocabulary — operators name their own source kinds).
  ``never_ingest_classes`` is an optional list of write-refusal class slugs
  (issue athenaeum#968); see :data:`NEVER_INGEST_CLASS_SLUGS` and
  :mod:`athenaeum.never_ingest` for the enforcement mechanism.

A malformed manifest (missing ``version``, wrong version, non-list
``sources``, a source missing a required field, a duplicate ``slug``, or
unparseable YAML) raises :class:`AuthorityManifestError` with a message
naming the specific defect — never a bare stack trace.

Layering: L1 (data model — reads/writes wiki frontmatter via
:mod:`athenaeum.models`, the L1 hub). No config-layer or LLM imports.
Factoring rule: this module owns ONLY the manifest schema + loader, the
duplicate-detector lookup, and the pointer-stub converter; it deliberately
does NOT wire into any reasoning-tier consumption path or run against the
live corpus — those are separate, later concerns (see the module docstring's
opening paragraph) layered on top by their own call sites.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from athenaeum.models import parse_frontmatter, render_frontmatter

log = logging.getLogger(__name__)

#: The only schema version this loader understands (issue athenaeum#426, slice 1).
SUPPORTED_MANIFEST_VERSION = 1

#: The frontmatter flag stamped on a converted pointer stub (issue athenaeum#426).
#: Consulted by :mod:`athenaeum.wiki_dedupe` (merge-eligibility) and
#: :mod:`athenaeum.search` (embed-input) so a stub is excluded from both by
#: construction rather than by a second ad hoc check at each call site.
POINTER_STUB_FLAG = "pointer_stub"

#: Never-ingest class slugs (issue athenaeum#968) -- a CLOSED vocabulary an
#: operator's ``never_ingest_classes:`` manifest entry must draw from. Naming
#: anything else is a malformed manifest (loud, same discipline as every
#: other defect this module rejects) -- see :func:`parse_authority_manifest`.
#:
#: - ``mirror-of-live-source``: the claim names a value whose system of
#:   record is a repo/config/doc already declared as a ``sources:`` entry
#:   here -- refuse it at intake exactly like :func:`find_duplicate_source`
#:   already flags it post-hoc for a compiled wiki page. Detected by
#:   :func:`athenaeum.never_ingest.classify_never_ingest`, which reuses THIS
#:   module's own topic-index lookup (:func:`find_duplicate_source`), never a
#:   second matcher.
#: - ``pending-state-todo``: the claim asserts the current presence/absence
#:   of something in an external artifact ("X needs updating", "has Y landed
#:   yet") -- a TODO belongs in the tracker of the artifact's own repo, not
#:   in memory. Detected by a small closed phrase list, also in
#:   :mod:`athenaeum.never_ingest`.
#:
#: Both classes are seed evidence from athenaeum#968's own filing comment (the
#: 2026-08-07 operator evidence log of three witnessed live pages).
CLASS_MIRROR_OF_LIVE_SOURCE = "mirror-of-live-source"
CLASS_PENDING_STATE_TODO = "pending-state-todo"
NEVER_INGEST_CLASS_SLUGS: frozenset[str] = frozenset(
    {CLASS_MIRROR_OF_LIVE_SOURCE, CLASS_PENDING_STATE_TODO}
)


class AuthorityManifestError(ValueError):
    """Raised when the authority manifest is missing required structure.

    Loud by design (mirrors :class:`athenaeum.storage.StorageConfigError` /
    :class:`athenaeum.screening.ScreeningConfigError`): a malformed manifest
    must never be silently treated as "no authoritative sources configured"
    — that would make every duplicate-detector call silently inert.
    """


@dataclass(frozen=True)
class AuthoritySource:
    """One authoritative live source and the topics/slugs it owns."""

    slug: str
    location: str
    topics: tuple[str, ...]
    kind: str = ""

    def topics_norm(self) -> frozenset[str]:
        """Normalized (case-folded, trimmed) topic set for membership tests."""
        return frozenset(_normalize_topic(t) for t in self.topics)


@dataclass(frozen=True)
class AuthorityManifest:
    """A loaded, validated authority manifest.

    ``never_ingest_classes`` (issue athenaeum#968) is the write-refusal class
    list the intake path consults -- empty by default (a manifest that omits
    the key, or every pre-athenaeum#968 manifest on disk, enforces nothing new;
    the gate is dark until an operator explicitly opts a class in). Each
    entry is one of :data:`NEVER_INGEST_CLASS_SLUGS`; see
    :mod:`athenaeum.never_ingest` for the detectors and the intake choke
    point that consults this field.
    """

    version: int
    sources: tuple[AuthoritySource, ...]
    never_ingest_classes: tuple[str, ...] = ()

    def topic_index(self) -> dict[str, AuthoritySource]:
        """Return a ``{normalized_topic: source}`` lookup map.

        Topics are matched case-insensitively with surrounding whitespace
        stripped (the detector's whole contract is deterministic LOOKUP, not
        fuzzy/semantic matching — normalization here is limited to the
        minimum needed so ``Lean-Development-Workflow`` and
        ``lean-development-workflow `` are treated as the same key).
        """
        index: dict[str, AuthoritySource] = {}
        for source in self.sources:
            for topic in source.topics:
                index[_normalize_topic(topic)] = source
        return index


def _normalize_topic(topic: str) -> str:
    return topic.strip().lower()


def _is_qualified_topic(normalized_topic: str) -> bool:
    """True when a topic is *qualified* rather than a bare single word (athenaeum#488).

    A qualified topic carries a discriminator beyond a lone entity token — it
    contains whitespace (``spartacus persona``) or a hyphen
    (``lean-development-workflow``, ``product-strategy persona``). A bare
    single word (``spartacus``) is treated as an *entity-level* token.

    This gate is applied ONLY to ``name``-derived matches (see
    :func:`_page_duplicate_candidates`): a page whose entire ``name`` is a
    lone entity token (e.g. a 20KB accumulated ``Spartacus`` entity record)
    is the entity, not a topical summary of a source, and must not be flagged
    as a duplicate on that name alone — the false positive AC2 of athenaeum#488 forbids
    (acting on it would destroy the richest record in the wiki). Explicit
    ``topics``/``topic``/``tags`` metadata is the author's deliberate claim of
    subject and is matched without this gate, exactly as before athenaeum#488.
    """
    return " " in normalized_topic or "-" in normalized_topic


def _require_nonempty_str(value: Any, field_name: str, *, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AuthorityManifestError(
            f"authority manifest: {where} has a missing/empty {field_name!r} field"
        )
    return value.strip()


def parse_authority_manifest(text: str) -> AuthorityManifest:
    """Parse + validate manifest YAML text into an :class:`AuthorityManifest`.

    Raises :class:`AuthorityManifestError` with a specific, human-readable
    message on any malformed input: unparseable YAML, a non-mapping
    top-level document, a missing/wrong ``version``, a non-list ``sources``,
    a source entry missing a required field, an empty ``topics`` list, or a
    duplicate ``slug`` across sources. Never raises a bare
    :class:`yaml.YAMLError` or ``KeyError``/``TypeError`` to the caller.
    """
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise AuthorityManifestError(
            f"authority manifest: invalid YAML ({exc})"
        ) from exc

    if raw is None:
        raise AuthorityManifestError("authority manifest: empty document")
    if not isinstance(raw, dict):
        raise AuthorityManifestError(
            "authority manifest: top-level document must be a mapping "
            f"(got {type(raw).__name__})"
        )

    version = raw.get("version")
    if version != SUPPORTED_MANIFEST_VERSION:
        raise AuthorityManifestError(
            f"authority manifest: unsupported version {version!r} "
            f"(expected {SUPPORTED_MANIFEST_VERSION})"
        )

    raw_sources = raw.get("sources")
    if not isinstance(raw_sources, list) or not raw_sources:
        raise AuthorityManifestError(
            "authority manifest: 'sources' must be a non-empty list"
        )

    sources: list[AuthoritySource] = []
    seen_slugs: set[str] = set()
    for idx, entry in enumerate(raw_sources):
        where = f"sources[{idx}]"
        if not isinstance(entry, dict):
            raise AuthorityManifestError(
                f"authority manifest: {where} must be a mapping "
                f"(got {type(entry).__name__})"
            )
        slug = _require_nonempty_str(entry.get("slug"), "slug", where=where)
        if slug in seen_slugs:
            raise AuthorityManifestError(
                f"authority manifest: duplicate source slug {slug!r}"
            )
        seen_slugs.add(slug)
        location = _require_nonempty_str(
            entry.get("location"), "location", where=where
        )
        kind_raw = entry.get("kind", "")
        kind = str(kind_raw).strip() if kind_raw else ""

        raw_topics = entry.get("topics")
        if not isinstance(raw_topics, list) or not raw_topics:
            raise AuthorityManifestError(
                f"authority manifest: {where} ({slug!r}) 'topics' must be a "
                "non-empty list"
            )
        topics: list[str] = []
        for t_idx, topic in enumerate(raw_topics):
            if not isinstance(topic, str) or not topic.strip():
                raise AuthorityManifestError(
                    f"authority manifest: {where} ({slug!r}) topics[{t_idx}] "
                    "must be a non-empty string"
                )
            topics.append(topic.strip())

        sources.append(
            AuthoritySource(
                slug=slug, location=location, topics=tuple(topics), kind=kind
            )
        )

    never_ingest_classes = _parse_never_ingest_classes(raw.get("never_ingest_classes"))

    return AuthorityManifest(
        version=version,
        sources=tuple(sources),
        never_ingest_classes=never_ingest_classes,
    )


def _parse_never_ingest_classes(raw_classes: Any) -> tuple[str, ...]:
    """Validate + normalize the optional ``never_ingest_classes:`` key.

    Absent/``None`` -> ``()`` (issue athenaeum#968's "dark by default": a
    manifest that never mentions this key enforces nothing new). Present but
    not a list, an entry that is not a non-empty string, an entry outside
    :data:`NEVER_INGEST_CLASS_SLUGS`, or a duplicate entry all raise
    :class:`AuthorityManifestError` -- same "loud on malformed" contract as
    every other field this loader validates.
    """
    if raw_classes is None:
        return ()
    if not isinstance(raw_classes, list):
        raise AuthorityManifestError(
            "authority manifest: 'never_ingest_classes' must be a list "
            f"(got {type(raw_classes).__name__})"
        )
    classes: list[str] = []
    seen: set[str] = set()
    for idx, entry in enumerate(raw_classes):
        if not isinstance(entry, str) or not entry.strip():
            raise AuthorityManifestError(
                f"authority manifest: never_ingest_classes[{idx}] must be a "
                "non-empty string"
            )
        slug = entry.strip()
        if slug not in NEVER_INGEST_CLASS_SLUGS:
            raise AuthorityManifestError(
                f"authority manifest: never_ingest_classes[{idx}] "
                f"{slug!r} is not a recognised class (expected one of "
                f"{sorted(NEVER_INGEST_CLASS_SLUGS)})"
            )
        if slug in seen:
            raise AuthorityManifestError(
                f"authority manifest: duplicate never_ingest_classes entry "
                f"{slug!r}"
            )
        seen.add(slug)
        classes.append(slug)
    return tuple(classes)


def load_authority_manifest(path: Path) -> AuthorityManifest:
    """Load + validate the manifest at *path*.

    A missing file returns an EMPTY manifest (``version=1, sources=()``) —
    an unconfigured knowledge base has no authoritative sources registered
    yet, which is a legitimate, inert starting state, not an error. A file
    that EXISTS but is malformed raises :class:`AuthorityManifestError` —
    once an operator has started a manifest, a defect in it must be loud, not
    silently treated as "empty".
    """
    if not path.is_file():
        return AuthorityManifest(version=SUPPORTED_MANIFEST_VERSION, sources=())
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise AuthorityManifestError(
            f"authority manifest: could not read {path} ({exc})"
        ) from exc
    return parse_authority_manifest(text)


# ---------------------------------------------------------------------------
# Duplicate detector — deterministic lookup, not semantic similarity.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DuplicateMatch:
    """One memory page flagged as duplicating a manifest-listed source."""

    page_path: Path
    matched_topic: str
    source: AuthoritySource


def _page_duplicate_candidates(meta: dict[str, Any]) -> list[tuple[str, bool]]:
    """Collect ``(candidate_string, from_name)`` pairs from page frontmatter.

    Single source of truth for what a page declares as its subject, so
    :func:`find_duplicate_source` and :func:`find_duplicates_in_wiki` cannot
    drift apart (before athenaeum#488 each collected candidates independently).

    Explicit ``topics`` (list or scalar), ``topic`` (scalar), and ``tags``
    (list) entries are the author's deliberate subject claim and yield
    ``from_name=False``. The page ``name`` (issue athenaeum#488) is a weaker title
    signal and yields ``from_name=True`` so the caller can apply the
    qualified-topic gate to it — a bare single-word ``name`` on a rich entity
    page must not flag as a duplicate (AC2), while a qualified name such as
    ``Spartacus persona`` (matching the owned topic ``spartacus persona``)
    correctly does (AC1).
    """
    candidates: list[tuple[str, bool]] = []

    raw_topics = meta.get("topics")
    if isinstance(raw_topics, list):
        candidates.extend((str(t), False) for t in raw_topics if isinstance(t, str))
    elif isinstance(raw_topics, str) and raw_topics.strip():
        candidates.append((raw_topics, False))

    single_topic = meta.get("topic")
    if isinstance(single_topic, str) and single_topic.strip():
        candidates.append((single_topic, False))

    raw_tags = meta.get("tags")
    if isinstance(raw_tags, list):
        candidates.extend((str(t), False) for t in raw_tags if isinstance(t, str))

    name = meta.get("name")
    if isinstance(name, str) and name.strip():
        candidates.append((name, True))

    return candidates


def _match_duplicate(
    meta: dict[str, Any] | None,
    index: dict[str, AuthoritySource],
) -> tuple[AuthoritySource, str] | None:
    """Return ``(source, matched_topic)`` for the first candidate that hits.

    ``matched_topic`` is the ORIGINAL candidate string that matched (so the
    lint output can name what it saw). Name-derived candidates only match a
    *qualified* topic (:func:`_is_qualified_topic`); explicit topic/tag
    candidates match any owned topic. Returns ``None`` when nothing matches.
    """
    if not meta or not index:
        return None
    for candidate, from_name in _page_duplicate_candidates(meta):
        normalized = _normalize_topic(candidate)
        source = index.get(normalized)
        if source is None:
            continue
        if from_name and not _is_qualified_topic(normalized):
            continue
        return source, candidate
    return None


def find_duplicate_source(
    meta: dict[str, Any] | None,
    manifest: AuthorityManifest,
) -> AuthoritySource | None:
    """Return the :class:`AuthoritySource` a memory's frontmatter duplicates.

    Deterministic LOOKUP against the manifest's owned topics/slugs — NEVER
    semantic similarity. A memory page is considered to duplicate a live
    source when its frontmatter carries a ``topics:`` list (or a single
    ``topic:`` scalar), a ``tags:`` list, or a ``name:`` (issue athenaeum#488) with an
    entry that matches (case-insensitively, whitespace-trimmed) one of the
    manifest's owned topic strings for some source. A ``name`` matches only a
    *qualified* topic (see :func:`_is_qualified_topic`) so a bare entity name
    does not false-positive. Returns ``None`` when nothing matches (or *meta*
    is empty/missing) — a non-duplicate passes.
    """
    if not meta:
        return None
    match = _match_duplicate(meta, manifest.topic_index())
    return match[0] if match else None


def find_duplicates_in_wiki(
    wiki_root: Path,
    manifest: AuthorityManifest,
) -> list[DuplicateMatch]:
    """Scan ``wiki/*.md`` (top-level, non-underscore-prefixed) for duplicates.

    READ-ONLY — never mutates a page. Mirrors the shallow, top-level scan
    :func:`athenaeum.wiki_dedupe.discover_wiki_dedupe_candidates` uses (no
    subdirectory recursion, ``_``-prefixed sidecars excluded). Pages already
    converted to a pointer stub (``pointer_stub: true``) are skipped — a stub
    trivially "matches" its own pointed-at topic but is not a fresh duplicate
    needing conversion. Returns matches sorted by filename for deterministic
    output (the CLI lint's contract).
    """
    if not wiki_root.is_dir():
        return []
    index = manifest.topic_index()
    matches: list[DuplicateMatch] = []
    for path in sorted(wiki_root.glob("*.md")):
        if path.name.startswith("_"):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        meta, _body = parse_frontmatter(text)
        if not isinstance(meta, dict) or not meta:
            continue
        if is_pointer_stub(meta):
            continue
        match = _match_duplicate(meta, index)
        if match is None:
            continue
        source, matched_topic = match
        matches.append(
            DuplicateMatch(page_path=path, matched_topic=matched_topic, source=source)
        )
    return matches


def is_pointer_stub(meta: dict[str, Any] | None) -> bool:
    """True when frontmatter carries a truthy ``pointer_stub`` flag (athenaeum#426).

    Same coercion contract as :func:`athenaeum.models.parse_deprecated`:
    accepts a real bool or a truthy string variant; missing/falsey => False.
    Single source of truth for stub detection — consulted by
    :mod:`athenaeum.wiki_dedupe` (merge-eligibility exclusion) and
    :mod:`athenaeum.search` (embed-input truncation).
    """
    if not meta:
        return False
    value = meta.get(POINTER_STUB_FLAG)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes")
    return bool(value)


# ---------------------------------------------------------------------------
# Pointer-stub converter.
# ---------------------------------------------------------------------------


def pointer_stub_line(title: str, source: AuthoritySource) -> str:
    """Render the one-line pointer body: ``title`` + authoritative location.

    This single line is the ENTIRE stub body (see :func:`convert_to_pointer_stub`)
    and is also the only text a stub contributes to embeddings (issue athenaeum#426
    "stub hygiene") — recall still needs *something* findable, but nothing
    beyond the pointer.
    """
    return f"{title} — see {source.location} (authoritative: {source.slug})"


def convert_to_pointer_stub(
    text: str,
    source: AuthoritySource,
    *,
    title: str | None = None,
) -> str:
    """Convert a duplicating memory's full markdown text into a pointer stub.

    Not a bare delete — recall still needs to find the skill/source, so the
    result keeps the page's frontmatter (with ``pointer_stub: true`` added)
    and replaces the BODY with a single pointer line naming the title and the
    authoritative location. *title* overrides the frontmatter ``name``; when
    omitted, the frontmatter ``name`` is used, falling back to the source's
    slug if even that is absent.

    Idempotent: converting an already-converted stub again is a no-op shape
    (the flag is already true, the body is already the one pointer line for
    the same source/title).
    """
    meta, _body = parse_frontmatter(text)
    if not isinstance(meta, dict):
        meta = {}
    resolved_title = title or str(meta.get("name") or source.slug)
    meta = dict(meta)
    meta[POINTER_STUB_FLAG] = True
    new_body = pointer_stub_line(resolved_title, source) + "\n"
    return render_frontmatter(meta) + "\n" + new_body


def convert_page_to_pointer_stub(
    page_path: Path,
    source: AuthoritySource,
    *,
    title: str | None = None,
) -> str:
    """Read *page_path*, convert it to a pointer stub, and return the new text.

    Does NOT write the file — callers decide when/whether to persist
    (mirrors the read/transform/write split every other mutating helper in
    this codebase uses, e.g. :mod:`athenaeum.repair`). Library-callable
    convenience so a caller doesn't have to hand-roll the read step.
    """
    text = page_path.read_text(encoding="utf-8")
    return convert_to_pointer_stub(text, source, title=title)


__all__ = [
    "SUPPORTED_MANIFEST_VERSION",
    "POINTER_STUB_FLAG",
    "CLASS_MIRROR_OF_LIVE_SOURCE",
    "CLASS_PENDING_STATE_TODO",
    "NEVER_INGEST_CLASS_SLUGS",
    "AuthorityManifestError",
    "AuthoritySource",
    "AuthorityManifest",
    "DuplicateMatch",
    "parse_authority_manifest",
    "load_authority_manifest",
    "find_duplicate_source",
    "find_duplicates_in_wiki",
    "is_pointer_stub",
    "pointer_stub_line",
    "convert_to_pointer_stub",
    "convert_page_to_pointer_stub",
]
