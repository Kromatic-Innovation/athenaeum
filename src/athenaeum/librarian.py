"""Knowledge librarian — tiered compilation pipeline.

Processes raw intake files from a knowledge directory's raw/ folder into wiki
entity pages using a four-tier approach:

  Tier 1: Programmatic entity matching (no LLM)
  Tier 2: Classification via fast LLM
  Tier 3: Content writing via capable LLM
  Tier 4: Human escalation to _pending_questions.md

Usage:
  athenaeum run [--raw-root PATH] [--wiki-root PATH] [--dry-run]

Environment:
  ANTHROPIC_API_KEY          Required for Tier 2/3 LLM calls.
  ATHENAEUM_CLASSIFY_MODEL   Override the Tier 2 model (default: claude-haiku-4-5-20251001)
  ATHENAEUM_WRITE_MODEL      Override the Tier 3 model (default: claude-sonnet-4-6)
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from pathlib import Path

import anthropic

from athenaeum.models import (
    EntityAction,
    EntityIndex,
    ProcessingResult,
    RawFile,
    load_schema_list,
    parse_frontmatter,
)
from athenaeum.tiers import (
    tier1_programmatic_match,
    tier2_classify,
    tier3_write,
    tier4_escalate,
)

log = logging.getLogger("athenaeum")

# Defaults — can be overridden via CLI args or the run() API
DEFAULT_KNOWLEDGE_ROOT = Path.home() / "knowledge"
DEFAULT_RAW_ROOT = DEFAULT_KNOWLEDGE_ROOT / "raw"
DEFAULT_WIKI_ROOT = DEFAULT_KNOWLEDGE_ROOT / "wiki"

# Fallback valid values if schema files are missing
FALLBACK_TYPES = [
    "person", "company", "project", "concept", "tool",
    "reference", "source", "preference", "principle",
]
FALLBACK_ACCESS = ["open", "internal", "confidential", "personal"]
FALLBACK_TAGS = [
    "active", "archived", "blocked",
]

# Raw file naming: {timestamp}-{uuid8}.md
RAW_FILE_RE = re.compile(r"^(\d{8}T\d{6}Z?)-([0-9a-f]{8})\.md$", re.IGNORECASE)


def discover_raw_files(raw_root: Path) -> list[RawFile]:
    """Find all raw intake files, sorted by timestamp."""
    files: list[RawFile] = []
    if not raw_root.exists():
        return files

    for source_dir in sorted(raw_root.iterdir()):
        if not source_dir.is_dir():
            continue
        source = source_dir.name
        for fpath in sorted(source_dir.glob("*.md")):
            if fpath.name == ".gitkeep":
                continue
            m = RAW_FILE_RE.match(fpath.name)
            if m:
                files.append(RawFile(
                    path=fpath,
                    source=source,
                    timestamp=m.group(1),
                    uuid8=m.group(2),
                ))
            else:
                files.append(RawFile(
                    path=fpath,
                    source=source,
                    timestamp="",
                    uuid8="",
                ))
    return files


def rebuild_index(wiki_root: Path) -> None:
    """Rebuild _index.md from all entity pages in the wiki."""
    from datetime import date

    by_type: dict[str, list[tuple[str, str, str]]] = {}
    for fpath in sorted(wiki_root.glob("*.md")):
        if fpath.name.startswith("_"):
            continue
        try:
            text = fpath.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        meta, _ = parse_frontmatter(text)
        if not meta or not meta.get("name"):
            continue
        etype = meta.get("type", "unknown")
        uid = meta.get("uid", "")
        name = meta.get("name", fpath.stem)
        by_type.setdefault(etype, []).append((name, uid, fpath.name))

    lines = [
        "# Knowledge Wiki Index",
        "",
        "Auto-maintained by the knowledge librarian. Lists all entity pages",
        "grouped by type.",
        "",
        f"_Last updated: {date.today().isoformat()}_",
        f"_Total entities: {sum(len(v) for v in by_type.values())}_",
        "",
    ]
    for etype in sorted(by_type.keys()):
        lines.append(f"## {etype.title()}")
        lines.append("")
        for name, uid, filename in sorted(by_type[etype], key=lambda x: x[0].lower()):
            label = f"`{uid}` " if uid else ""
            lines.append(f"- {label}[{name}]({filename})")
        lines.append("")

    (wiki_root / "_index.md").write_text("\n".join(lines), encoding="utf-8")
    log.info("Rebuilt _index.md with %d entities", sum(len(v) for v in by_type.values()))


def git_snapshot(knowledge_root: Path, message: str) -> bool:
    """Stage all changes and commit if there are any. Returns True if committed."""
    if not (knowledge_root / ".git").exists():
        log.warning("No .git in %s — skipping git snapshot", knowledge_root)
        return False

    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(knowledge_root),
        capture_output=True, text=True,
    )
    if not result.stdout.strip():
        return False

    subprocess.run(
        ["git", "add", "-A"],
        cwd=str(knowledge_root), check=True,
    )
    subprocess.run(
        ["git", "commit", "-m", message],
        cwd=str(knowledge_root), check=True,
    )
    log.info("Git commit: %s", message)
    return True


def process_one(
    raw: RawFile,
    index: EntityIndex,
    wiki_root: Path,
    client: anthropic.Anthropic | None,
    valid_types: list[str],
    valid_tags: list[str],
    valid_access: list[str],
    dry_run: bool = False,
) -> ProcessingResult:
    """Process a single raw file through all tiers."""
    result = ProcessingResult(raw_file=raw)

    # --- Tier 1: Programmatic matching ---
    matched = tier1_programmatic_match(raw, index)
    matched_names = [name for name, _, _ in matched]

    for name, uid_or_name, fpath in matched:
        if index.has_entity_format(fpath):
            log.info("  T1 match (entity format): %s → %s", name, fpath.name)
        else:
            log.info("  T1 match (old format, skip): %s → %s", name, fpath.name)
            result.skipped.append(name)

    if dry_run:
        log.info(
            "  [DRY RUN] T1 matched %d, skipped %d — LLM tiers skipped",
            len(matched), len(result.skipped),
        )
        log.info("  [DRY RUN] Raw content preview: %s", raw.content[:120].replace("\n", " "))
        return result

    # --- Tier 2: Classification ---
    classified = tier2_classify(
        raw, matched_names, valid_types, valid_tags, valid_access, client,
    )
    log.info("  T2 classified %d new entities", len(classified))

    # Build actions
    actions: list[EntityAction] = []
    for c in classified:
        actions.append(EntityAction(
            kind="create",
            name=c.name,
            entity_type=c.entity_type,
            tags=c.tags,
            access=c.access,
            existing_uid=c.existing_uid,
            observations=c.observations or raw.content[:2000],
        ))

    for name, uid_or_name, fpath in matched:
        if index.has_entity_format(fpath):
            actions.append(EntityAction(
                kind="update",
                name=name,
                entity_type="",
                tags=[],
                access="",
                existing_uid=uid_or_name,
                observations=raw.content[:2000],
            ))

    if not actions:
        log.info("  No actions needed for %s", raw.ref)
        return result

    # --- Tier 3: Content writing ---
    assert client is not None, "client required for non-dry-run"
    new_entities, updated_uids, escalations = tier3_write(
        raw, actions, index, wiki_root, client,
    )

    for entity in new_entities:
        page_path = wiki_root / entity.filename
        page_path.write_text(entity.render(), encoding="utf-8")
        index.register(entity)
        result.created.append(entity)
        log.info("  Created: %s → %s", entity.name, entity.filename)

    result.updated.extend(updated_uids)
    result.escalated.extend(escalations)

    # --- Tier 4: Escalation ---
    if escalations:
        tier4_escalate(escalations, wiki_root / "_pending_questions.md")

    return result


def run(
    raw_root: Path = DEFAULT_RAW_ROOT,
    wiki_root: Path = DEFAULT_WIKI_ROOT,
    knowledge_root: Path = DEFAULT_KNOWLEDGE_ROOT,
    dry_run: bool = False,
    max_files: int = 50,
    max_api_calls: int = 200,
) -> int:
    """Run the librarian pipeline. Returns 0 on success, 1 on error."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key and not dry_run:
        log.error("ANTHROPIC_API_KEY not set (required unless dry_run=True)")
        return 1

    if not wiki_root.exists():
        log.error("Wiki root does not exist: %s", wiki_root)
        return 1

    if not dry_run and not (knowledge_root / ".git").exists():
        log.error(
            "No .git in %s — refusing to run without a writable git repo. "
            "The librarian's pre-processing snapshot is load-bearing for raw-file "
            "recovery. Either point knowledge_root at a real git repo, or pass "
            "dry_run=True to inspect without writing.",
            knowledge_root,
        )
        return 1

    raw_files = discover_raw_files(raw_root)
    if not raw_files:
        log.info("No raw files to process. Nothing to do.")
        return 0

    log.info("Found %d raw file(s) to process", len(raw_files))

    if len(raw_files) > max_files:
        log.info(
            "Budget cap: processing %d of %d files this run",
            max_files, len(raw_files),
        )
        raw_files = raw_files[:max_files]

    schema_path = wiki_root / "_schema"
    valid_types = load_schema_list(schema_path, "types.md") or FALLBACK_TYPES
    valid_tags = load_schema_list(schema_path, "tags.md") or FALLBACK_TAGS
    valid_access = load_schema_list(schema_path, "access-levels.md") or FALLBACK_ACCESS

    index = EntityIndex(wiki_root)
    log.info("Loaded %d wiki entries into index", len(index._by_name))

    client: anthropic.Anthropic | None = (
        anthropic.Anthropic(api_key=api_key, max_retries=3) if api_key else None
    )

    if not dry_run:
        git_snapshot(knowledge_root, "librarian: pre-processing snapshot")

    total_created = 0
    total_updated = 0
    total_escalated = 0
    total_skipped = 0
    total_api_calls = 0
    failed_files: list[str] = []

    for raw in raw_files:
        if not dry_run and total_api_calls >= max_api_calls:
            log.warning(
                "API call budget exhausted (%d/%d) — stopping early",
                total_api_calls, max_api_calls,
            )
            break

        log.info("Processing: %s", raw.ref)
        try:
            result = process_one(
                raw, index, wiki_root, client,
                valid_types, valid_tags, valid_access,
                dry_run=dry_run,
            )
        except Exception:
            log.exception("Failed to process %s", raw.ref)
            failed_files.append(raw.ref)
            continue

        # Estimate API calls: 1 for classify + 1 per created + 1 per updated
        calls_this_file = 1 + len(result.created) + len(result.updated) if not dry_run else 0
        total_api_calls += calls_this_file

        total_created += len(result.created)
        total_updated += len(result.updated)
        total_escalated += len(result.escalated)
        total_skipped += len(result.skipped)

        if not dry_run:
            raw.path.unlink()
            log.info("  Deleted: %s", raw.path)

    log.info(
        "Done: %d created, %d updated, %d escalated, %d skipped, %d failed, ~%d API calls",
        total_created, total_updated, total_escalated, total_skipped,
        len(failed_files), total_api_calls,
    )

    if not dry_run and (total_created > 0 or total_updated > 0):
        rebuild_index(wiki_root)

    if not dry_run:
        msg = (
            f"librarian: processed {len(raw_files)} file(s) "
            f"({total_created}C {total_updated}U {total_escalated}E {len(failed_files)}F)"
        )
        git_snapshot(knowledge_root, msg)

    if failed_files:
        log.warning("Failed files (will retry next run): %s", ", ".join(failed_files))
        return 1

    return 0
