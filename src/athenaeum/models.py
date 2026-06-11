# SPDX-License-Identifier: Apache-2.0
"""Data models, YAML frontmatter parsing, and entity index for Athenaeum."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from collections.abc import ItemsView, Iterator

import yaml

# --- UID generation ---


def generate_uid() -> str:
    """Generate an 8-character hex UID from uuid4."""
    return uuid.uuid4().hex[:8]


def slugify(name: str) -> str:
    """Convert a name to a filesystem-safe slug."""
    slug = name.lower().strip()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    slug = slug.strip("-")
    return slug[:60]  # cap length


# --- Frontmatter parsing ---

_FM_RE = re.compile(r"^---\s*\r?\n(.*?)\r?\n---\s*\r?\n", re.DOTALL)


def parse_frontmatter(text: str) -> tuple[dict[str, object], str]:
    """Split YAML frontmatter from body. Returns ``(metadata, body)``.

    The metadata dict has string keys and arbitrary YAML-scalar/list/dict
    values (hence ``object``). Callers that need narrower types should
    validate the fields they depend on — the schema is intentionally
    open so non-core frontmatter keys round-trip cleanly.
    """
    m = _FM_RE.match(text)
    if not m:
        return {}, text
    try:
        meta = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        return {}, text
    # Coerce identity fields at the YAML boundary. PyYAML loads bare
    # all-decimal hex uids (e.g. ``19052``) and unquoted numeric names
    # as ``int`` — downstream code (schema validation, index lookup,
    # filename rendering) expects ``str``. Fixing it here keeps the
    # on-disk dict consistent with the model and removes the need for
    # int-coercion shims further down.
    if isinstance(meta, dict):
        for _k in ("uid", "type", "name"):
            _v = meta.get(_k)
            if isinstance(_v, int) and not isinstance(_v, bool):
                meta[_k] = str(_v)
    body = text[m.end() :]
    return meta, body


def parse_refines(meta: dict[str, object] | None) -> list[str]:
    """Coerce a frontmatter ``refines:`` value into a clean list of slugs.

    Accepts:
    - ``None`` / missing key → ``[]``.
    - ``list[str]`` of memory ``name:`` slugs (the documented shape).

    Raises:
        ValueError: when ``refines`` is present but not a list, or any
            entry is not a non-empty string. The frontmatter is a
            durable contract — a typo (``refines: name-x`` rendered as a
            scalar) should be loud, not silent.
    """
    if not meta:
        return []
    raw = meta.get("refines")
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError(
            f"refines must be a list of memory name slugs, got {type(raw).__name__}"
        )
    out: list[str] = []
    for entry in raw:
        if not isinstance(entry, str) or not entry.strip():
            raise ValueError(
                f"refines entries must be non-empty strings, got {entry!r}"
            )
        out.append(entry.strip())
    return out


def parse_supersedes(meta: dict[str, object] | None) -> list[dict[str, str]]:
    """Coerce a frontmatter ``supersedes:`` value into a list of records.

    Accepts:
    - ``None`` / missing key → ``[]``.
    - ``list[dict]`` of ``{name, as_of, reason}`` records. ``name`` is
      required and must be a non-empty string. ``as_of`` and ``reason``
      are optional; missing values are stored as empty strings so
      downstream consumers can rely on the keys existing.

    Raises:
        ValueError: when ``supersedes`` is not a list, an entry is not a
            mapping, or an entry lacks a non-empty ``name`` key.
    """
    if not meta:
        return []
    raw = meta.get("supersedes")
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError(
            f"supersedes must be a list of records, got {type(raw).__name__}"
        )
    out: list[dict[str, str]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise ValueError(
                f"supersedes entries must be mappings, got {type(entry).__name__}"
            )
        name = entry.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("supersedes entries require a non-empty 'name' key")
        as_of = entry.get("as_of", "")
        reason = entry.get("reason", "")
        out.append(
            {
                "name": name.strip(),
                "as_of": str(as_of) if as_of is not None else "",
                "reason": str(reason) if reason is not None else "",
            }
        )
    return out


def parse_superseded_by(meta: dict[str, object] | None) -> str:
    """Return the frontmatter ``superseded_by`` pointer (winner name slug), or "".

    Set by the resolver's keep_a/keep_b enactment on the LOSING member to
    mark it as valid-then-replaced history. Non-empty => the member is
    inactive (excluded from recall + C3 compile) but preserved on disk.
    Tolerant: a non-string value coerces to its str form; missing => "".
    """
    if not meta:
        return ""
    raw = meta.get("superseded_by")
    if raw is None:
        return ""
    return str(raw).strip()


def parse_deprecated(meta: dict[str, object] | None) -> bool:
    """Return the truthy ``deprecated`` frontmatter flag (deprecate_both, #191).

    Accepts a real bool, or a string variant (``true``/``1``/``yes``,
    case-insensitive); any other truthy value coerces via ``bool``.
    Missing / falsey => ``False``.
    """
    if not meta:
        return False
    dep = meta.get("deprecated")
    if isinstance(dep, bool):
        return dep
    if isinstance(dep, str):
        return dep.strip().lower() in ("true", "1", "yes")
    return bool(dep)


def is_inactive_memory(meta: dict[str, object] | None) -> bool:
    """True when a memory file is marked inactive and must not surface as a live claim.

    Inactive == frontmatter declares EITHER a non-empty ``superseded_by``
    (keep_a/keep_b loser, issue #191) OR a truthy ``deprecated`` flag
    (deprecate_both, issue #191). Inactive members are preserved on disk
    for audit but are skipped by recall (search index) and by the C3 merge
    compile so their claims drop out of the live wiki.
    """
    if not meta:
        return False
    if parse_superseded_by(meta):
        return True
    return parse_deprecated(meta)


def render_frontmatter(meta: dict[str, object]) -> str:
    """Render a dict as a YAML frontmatter block.

    Contract: key order preserved (``sort_keys=False``) for tier0
    byte-for-byte round-trip. Do not change without updating
    ``test_render_frontmatter_preserves_key_order``.
    """
    dumped = yaml.dump(
        meta, default_flow_style=False, sort_keys=False, allow_unicode=True
    )
    return f"---\n{dumped}---\n"


# --- Data classes ---


@dataclass
class RawFile:
    """A raw intake file from raw/{source}/{timestamp}-{uuid8}.md."""

    path: Path
    source: str
    timestamp: str
    uuid8: str
    _content: str | None = field(default=None, repr=False)

    @property
    def content(self) -> str:
        if self._content is None:
            self._content = self.path.read_text(encoding="utf-8")
        return self._content

    @property
    def ref(self) -> str:
        """Short reference for footnotes."""
        return f"{self.source}/{self.path.name}"


@dataclass
class AutoMemoryFile:
    """A raw intake file from ``raw/auto-memory/<scope>/<prefix>_<slug>.md``.

    Parallel sibling to :class:`RawFile` — auto-memory uses a different
    naming convention (``feedback_*.md``, ``project_*.md``, ``reference_*.md``,
    ``user_*.md``, ``Recall_*.md``) and a different frontmatter schema
    (``type`` / ``originSessionId`` / ``originTurn`` / ``sources`` instead
    of the entity schema's ``uid`` / ``name``).

    ``origin_scope`` is the scope directory name verbatim — the full
    path-hash identifier (e.g. ``-Users-tristankromer-Code-voltaire``) or
    the literal ``_unscoped``. Preserving this on the record is C2/C3's
    routing key; the compile step downstream will carry it through to the
    wiki entry metadata.
    """

    path: Path
    origin_scope: str
    memory_type: str  # feedback|project|reference|user|recall
    name: str = ""
    description: str = ""
    origin_session_id: str | None = None
    origin_turn: int | None = None
    sources: list[str] = field(default_factory=list)
    # Lane 1 / #167: declared relationships to other memories. Both
    # default to empty list. ``refines`` lists ``name:`` slugs of
    # memories this one narrows (general + exception — BOTH stay
    # active). ``supersedes`` lists ``{name, as_of, reason}`` records
    # declaring this memory replaces another (the superseded memory
    # stays for audit but is no longer active guidance). Matching is
    # by ``name:`` slug, not path.
    refines: list[str] = field(default_factory=list)
    supersedes: list[dict[str, str]] = field(default_factory=list)
    # Issue #191: non-destructive inactive markers written by the resolver's
    # keep_a/keep_b (superseded_by = winner name) and deprecate_both
    # (deprecated = True) enactment. An inactive member is preserved on disk
    # for audit but excluded from recall + the C3 compile so it does not
    # resurface as a live claim.
    superseded_by: str = ""
    deprecated: bool = False
    _content: str | None = field(default=None, repr=False)

    @property
    def content(self) -> str:
        if self._content is None:
            self._content = self.path.read_text(encoding="utf-8")
        return self._content

    @property
    def ref(self) -> str:
        """Short reference for footnotes — scope/filename."""
        return f"{self.origin_scope}/{self.path.name}"

    def is_inactive(self) -> bool:
        """True when this member carries a #191 inactive marker."""
        return bool(self.superseded_by) or self.deprecated

    def supersedes_names(self) -> list[str]:
        """Return just the ``name`` keys from :attr:`supersedes` records."""
        out: list[str] = []
        for rec in self.supersedes:
            if isinstance(rec, dict):
                n = rec.get("name")
                if isinstance(n, str) and n:
                    out.append(n)
        return out


@dataclass
class WikiEntity:
    """An entity page in wiki/ using the full entity template format."""

    uid: str
    type: str
    name: str
    aliases: list[str] = field(default_factory=list)
    access: str = "internal"
    tags: list[str] = field(default_factory=list)
    related: list[dict[str, str]] = field(default_factory=list)
    created: str = ""
    updated: str = ""
    body: str = ""
    # Per-claim provenance (issue #90 / #95). Optional so old wikis
    # without provenance still round-trip cleanly. ``source`` is the
    # wiki-level default; ``field_sources`` overrides per field.
    source: str | dict | None = None
    # ``field_sources`` per-field value is ``str``/``dict`` (legacy)
    # OR ``list[dict]`` of ``{"value", "source"}`` records (per-value
    # attribution for list fields, issue #102).
    field_sources: dict[str, str | dict | list] | None = None

    @property
    def filename(self) -> str:
        return f"{self.uid}-{slugify(self.name)}.md"

    def render(self) -> str:
        """Render to full markdown with YAML frontmatter."""
        meta: dict = {
            "uid": self.uid,
            "type": self.type,
            "name": self.name,
        }
        if self.aliases:
            meta["aliases"] = self.aliases
        meta["access"] = self.access
        if self.tags:
            meta["tags"] = self.tags
        if self.related:
            meta["related"] = self.related
        if self.created:
            meta["created"] = self.created
        if self.updated:
            meta["updated"] = self.updated
        if self.source is not None:
            meta["source"] = self.source
        if self.field_sources:
            meta["field_sources"] = self.field_sources
        return render_frontmatter(meta) + "\n" + self.body


@dataclass
class ClassifiedEntity:
    """Output of Tier 2 classification."""

    name: str
    entity_type: str
    tags: list[str]
    access: str
    is_new: bool
    existing_uid: str | None = None
    observations: str = ""


@dataclass
class EntityAction:
    """A create or update action for Tier 3."""

    kind: Literal["create", "update"]
    name: str
    entity_type: str
    tags: list[str]
    access: str
    existing_uid: str | None
    observations: str


@dataclass
class EscalationItem:
    """An item to escalate to _pending_questions.md."""

    raw_ref: str
    entity_name: str
    conflict_type: str  # "principled" | "ambiguous" | "classification_failed"
    description: str
    # Optional resolver proposal threaded through from
    # :func:`athenaeum.resolutions.propose_resolution`. When present and
    # confidence >= the configured threshold, :func:`tier4_escalate`
    # auto-applies the resolution to the rendered block. Typed as
    # ``Any`` to avoid a circular import (resolutions.py imports
    # AutoMemoryFile from this module). The runtime type is
    # ``athenaeum.resolutions.ResolutionProposal | None``.
    proposal: Any = None
    # Absolute paths of the flagged member files in resolver ``a``/``b``
    # order (``members[0]`` is side ``a``, ``members[1]`` is side ``b``).
    # Populated by :func:`athenaeum.merge._emit_escalation` so the
    # enactment lane (#166 follow-up) can DELETE the target member when a
    # high-confidence ``forget_*`` / ``correct_*`` verdict auto-applies.
    # Empty for non-source-attributed escalations (the enactment lane then
    # no-ops). Stored as strings to keep the dataclass trivially copyable.
    members: list[str] = field(default_factory=list)


@dataclass
class TokenUsage:
    """Accumulated API token usage for a pipeline run."""

    input_tokens: int = 0
    output_tokens: int = 0
    api_calls: int = 0
    # Prompt-caching counters (issue #230). ``input_tokens`` from the API
    # excludes cached tokens, so these accumulate separately: creation is
    # billed at ~1.25x the input rate, reads at ~0.1x.
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0

    def add(
        self,
        input_tokens: int,
        output_tokens: int,
        cache_creation_input_tokens: int = 0,
        cache_read_input_tokens: int = 0,
    ) -> None:
        """Record tokens from one API call."""
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.cache_creation_input_tokens += cache_creation_input_tokens
        self.cache_read_input_tokens += cache_read_input_tokens
        self.api_calls += 1

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def estimated_cost_usd(self) -> float:
        """Estimate cost using Haiku/Sonnet blended rates.

        Uses a conservative blended rate: $1.50/M input, $7.50/M output.
        """
        return (
            self.input_tokens * 1.50 / 1_000_000 + self.output_tokens * 7.50 / 1_000_000
        )


def cache_usage_counts(response: object) -> tuple[int, int, int, int]:
    """Extract token counts from an Anthropic API response (issue #230).

    Returns ``(input_tokens, output_tokens, cache_creation_input_tokens,
    cache_read_input_tokens)``. Missing or non-int fields coerce to 0 so
    callers can log/accumulate without guarding against older SDK shapes
    or test doubles that omit the cache fields.
    """
    usage = getattr(response, "usage", None)

    def _count(name: str) -> int:
        value = getattr(usage, name, 0)
        return value if isinstance(value, int) and not isinstance(value, bool) else 0

    return (
        _count("input_tokens"),
        _count("output_tokens"),
        _count("cache_creation_input_tokens"),
        _count("cache_read_input_tokens"),
    )


@dataclass
class ProcessingResult:
    """Result of processing one raw file."""

    raw_file: RawFile
    created: list[WikiEntity] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    escalated: list[EscalationItem] = field(default_factory=list)


# --- Schema loading ---


def load_schema_list(schema_path: Path, filename: str) -> list[str]:
    """Load a list of valid values from a schema markdown table.

    Parses standard markdown tables, extracting the first cell from each
    data row. Header and separator rows are skipped.
    """
    fpath = schema_path / filename
    if not fpath.exists():
        return []
    text = fpath.read_text(encoding="utf-8")
    lines = text.splitlines()
    values: list[str] = []
    # Collect separator row indices so we can skip headers
    separator_indices: set[int] = set()
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("|") and all(c in "-| " for c in stripped):
            separator_indices.add(i)

    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        # Skip separator rows
        if i in separator_indices:
            continue
        # Skip header rows (the row immediately before a separator)
        if (i + 1) in separator_indices:
            continue
        cells = [c.strip() for c in stripped.split("|")]
        for cell in cells:
            if cell:
                values.append(cell)
                break
    return values


# --- Entity Index ---


class EntityIndex:
    """In-memory index of all wiki entities for name/alias lookup."""

    def __init__(self, wiki_root: Path) -> None:
        self.wiki_root = wiki_root
        self._by_name: dict[str, tuple[str, Path]] = {}
        self._entities: dict[str, dict] = {}
        self._by_uid: dict[str, Path] = {}
        self._entity_format_paths: set[Path] = set()
        self._load()

    def _load(self) -> None:
        for fpath in sorted(self.wiki_root.glob("*.md")):
            if fpath.name.startswith("_"):
                continue
            try:
                text = fpath.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            meta, _ = parse_frontmatter(text)
            if not meta:
                continue

            uid = meta.get("uid", "")
            name = meta.get("name", "")
            if not name:
                continue

            key = name.lower()
            self._by_name[key] = (uid or name, fpath)
            if uid:
                self._entities[uid] = meta
                self._by_uid[uid] = fpath
                self._entity_format_paths.add(fpath)

            for alias in meta.get("aliases", []):
                if alias:
                    self._by_name[alias.lower()] = (uid or name, fpath)

    def lookup(self, name: str) -> tuple[str, Path] | None:
        """Look up by name or alias (case-insensitive). Returns (uid_or_name, path) or None."""
        return self._by_name.get(name.lower())

    def get_by_uid(self, uid: str) -> Path | None:
        """Look up entity file path by UID. Returns None if not found."""
        return self._by_uid.get(uid)

    def has_entity_format(self, path: Path) -> bool:
        """Check if a wiki page uses the full entity template format (has uid field)."""
        return path in self._entity_format_paths

    def register(self, entity: WikiEntity) -> None:
        """Add a newly created entity to the index."""
        key = entity.name.lower()
        path = self.wiki_root / entity.filename
        self._by_name[key] = (entity.uid, path)
        self._entities[entity.uid] = {
            "uid": entity.uid,
            "type": entity.type,
            "name": entity.name,
        }
        self._by_uid[entity.uid] = path
        self._entity_format_paths.add(path)
        for alias in entity.aliases:
            if alias:
                self._by_name[alias.lower()] = (entity.uid, path)

    def __len__(self) -> int:
        """Number of unique name/alias keys indexed."""
        return len(self._by_name)

    def items(self) -> "ItemsView[str, tuple[str, Path]]":
        """Iterate over ``(name_or_alias_key, (uid_or_name, path))`` pairs.

        Replaces direct access to ``_by_name`` from callers that need to
        walk the index (e.g. tier-based scans). Returns a live view — do
        not mutate the index mid-iteration.
        """
        return self._by_name.items()

    def __iter__(self) -> "Iterator[str]":
        """Iterate over indexed name/alias keys."""
        return iter(self._by_name)
