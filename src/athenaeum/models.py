"""Data models, YAML frontmatter parsing, and entity index for Athenaeum."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path

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

_FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Split YAML frontmatter from body. Returns (metadata, body)."""
    m = _FM_RE.match(text)
    if not m:
        return {}, text
    try:
        meta = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        return {}, text
    body = text[m.end():]
    return meta, body


def render_frontmatter(meta: dict) -> str:
    """Render a dict as YAML frontmatter block."""
    dumped = yaml.dump(meta, default_flow_style=False, sort_keys=False, allow_unicode=True)
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
    kind: str  # "create" | "update"
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


@dataclass
class ProcessingResult:
    """Result of processing one raw file."""
    raw_file: RawFile
    created: list[WikiEntity] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)  # UIDs of updated entities
    skipped: list[str] = field(default_factory=list)
    escalated: list[EscalationItem] = field(default_factory=list)
    error: str | None = None


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
        if stripped.startswith("|") and all(
            c in "-| " for c in stripped
        ):
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

            for alias in meta.get("aliases", []):
                if alias:
                    self._by_name[alias.lower()] = (uid or name, fpath)

    def lookup(self, name: str) -> tuple[str, Path] | None:
        """Look up by name or alias (case-insensitive). Returns (uid_or_name, path) or None."""
        return self._by_name.get(name.lower())

    def has_entity_format(self, path: Path) -> bool:
        """Check if a wiki page uses the full entity template format (has uid field)."""
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return False
        meta, _ = parse_frontmatter(text)
        return bool(meta.get("uid"))

    def register(self, entity: WikiEntity) -> None:
        """Add a newly created entity to the index."""
        key = entity.name.lower()
        self._by_name[key] = (entity.uid, self.wiki_root / entity.filename)
        self._entities[entity.uid] = {
            "uid": entity.uid,
            "type": entity.type,
            "name": entity.name,
        }
        for alias in entity.aliases:
            if alias:
                self._by_name[alias.lower()] = (entity.uid, self.wiki_root / entity.filename)
