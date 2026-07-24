# SPDX-License-Identifier: Apache-2.0
"""Authority manifest + duplicate detector + pointer-stub converter (issue #426).

Stops memories from duplicating content a **live source** (a skill file, a
code path, a config file) already owns. A live source can drift out from
under a stale memory copy silently; a *pointer* that names the live location
cannot go stale in the same way — recall always resolves to whatever the
source currently says.

This module is the standalone, unit-testable slice: it builds the manifest
format + loader/validator, the lookup-based duplicate detector, and the
pointer-stub converter. It deliberately does NOT wire into any reasoning-tier
consumption path (that is #423's T1 duplicate bin / #432's T2 rejection) and
does NOT run against the live corpus (that is operator task #437) — see the
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

  ``version`` must be the literal integer ``1`` (schema-evolution seam — a
  future incompatible schema bumps it and the loader can dispatch on it).
  Each source requires ``slug`` (unique, non-empty), ``location``
  (non-empty — where the live source lives), and ``topics`` (non-empty list
  of non-empty strings). ``kind`` is optional free text (not validated
  against a closed vocabulary — operators name their own source kinds).

A malformed manifest (missing ``version``, wrong version, non-list
``sources``, a source missing a required field, a duplicate ``slug``, or
unparseable YAML) raises :class:`AuthorityManifestError` with a message
naming the specific defect — never a bare stack trace.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from athenaeum.models import parse_frontmatter, render_frontmatter

log = logging.getLogger(__name__)

#: The only schema version this loader understands (issue #426, slice 1).
SUPPORTED_MANIFEST_VERSION = 1

#: The frontmatter flag stamped on a converted pointer stub (issue #426).
#: Consulted by :mod:`athenaeum.wiki_dedupe` (merge-eligibility) and
#: :mod:`athenaeum.search` (embed-input) so a stub is excluded from both by
#: construction rather than by a second ad hoc check at each call site.
POINTER_STUB_FLAG = "pointer_stub"


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
    """A loaded, validated authority manifest."""

    version: int
    sources: tuple[AuthoritySource, ...]

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

    return AuthorityManifest(version=version, sources=tuple(sources))


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


def find_duplicate_source(
    meta: dict[str, Any] | None,
    manifest: AuthorityManifest,
) -> AuthoritySource | None:
    """Return the :class:`AuthoritySource` a memory's frontmatter duplicates.

    Deterministic LOOKUP against the manifest's owned topics/slugs — NEVER
    semantic similarity. A memory page is considered to duplicate a live
    source when its frontmatter carries a ``topics:`` list (or a single
    ``topic:`` scalar) or a ``tags:`` list with an entry that matches (case-
    insensitively, whitespace-trimmed) one of the manifest's owned topic
    strings for some source. Returns ``None`` when nothing matches (or
    *meta* is empty/missing) — a non-duplicate passes.
    """
    if not meta:
        return None
    index = manifest.topic_index()
    if not index:
        return None

    candidates: list[str] = []
    raw_topics = meta.get("topics")
    if isinstance(raw_topics, list):
        candidates.extend(str(t) for t in raw_topics if isinstance(t, str))
    elif isinstance(raw_topics, str) and raw_topics.strip():
        candidates.append(raw_topics)

    single_topic = meta.get("topic")
    if isinstance(single_topic, str) and single_topic.strip():
        candidates.append(single_topic)

    raw_tags = meta.get("tags")
    if isinstance(raw_tags, list):
        candidates.extend(str(t) for t in raw_tags if isinstance(t, str))

    for candidate in candidates:
        source = index.get(_normalize_topic(candidate))
        if source is not None:
            return source
    return None


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
        source = find_duplicate_source(meta, manifest)
        if source is None:
            continue
        candidates: list[str] = []
        raw_topics = meta.get("topics")
        if isinstance(raw_topics, list):
            candidates.extend(str(t) for t in raw_topics if isinstance(t, str))
        elif isinstance(raw_topics, str):
            candidates.append(raw_topics)
        single_topic = meta.get("topic")
        if isinstance(single_topic, str):
            candidates.append(single_topic)
        raw_tags = meta.get("tags")
        if isinstance(raw_tags, list):
            candidates.extend(str(t) for t in raw_tags if isinstance(t, str))
        matched_topic = next(
            (c for c in candidates if _normalize_topic(c) in source.topics_norm()),
            "",
        )
        matches.append(
            DuplicateMatch(page_path=path, matched_topic=matched_topic, source=source)
        )
    return matches


def is_pointer_stub(meta: dict[str, Any] | None) -> bool:
    """True when frontmatter carries a truthy ``pointer_stub`` flag (#426).

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
    and is also the only text a stub contributes to embeddings (issue #426
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
