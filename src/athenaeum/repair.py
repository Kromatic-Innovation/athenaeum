# SPDX-License-Identifier: Apache-2.0
"""YAML-frontmatter repair utilities.

Two corruption shapes are addressed, both originally introduced by older
versions of cwc-side enricher scripts:

1. **Tag-list indent splice** — Apollo enricher (pre-2026-05-08) spliced
   ``  - apollo:enriched`` (2-space) into a 0-space block list, producing
   mixed indentation that fails ``yaml.safe_load``.
2. **Unquoted-value YAML break** — older raw-tier writers emitted bare
   values starting with ``-`` or ``[`` for keys like ``title`` or
   ``organization_name``. YAML interprets these as block-sequence starts
   or flow-sequences and rejects the document.

Both functions default to dry-run (``apply=False``); idempotent on clean
trees. Ported from ``cwc/scripts/knowledge-librarian/repair_tag_indent.py``
and ``repair_yaml_value_quoting.py``.

Repair runs **before** schema validation by design: these passes work on
raw text via regex/line-walks because the corruption shapes prevent
``yaml.safe_load`` from producing a document at all; pydantic validators
are downstream consumers of already-parseable frontmatter.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class RepairReport:
    """Result of a repair scan/apply pass.

    Attributes:
        files_scanned: total ``.md`` files inspected (top-level only,
            ``_*`` files skipped — same convention as the original
            cwc scripts).
        files_changed: count that needed a fix (in dry-run, the count
            that *would* be fixed; in apply, the count that *was*).
        errors: list of ``(path, error-message)`` tuples for files that
            could not be read, parsed, or written.
        changes: list of ``(path, summary)`` tuples — one per fixed
            file. ``summary`` is a short human-readable description.
    """

    files_scanned: int = 0
    files_changed: int = 0
    errors: list[tuple[Path, str]] = field(default_factory=list)
    changes: list[tuple[Path, str]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Shared helpers


def _split_frontmatter(text: str) -> tuple[str, str] | None:
    """Return ``(frontmatter, body)`` or ``None`` if the file has none."""
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---\n", 4)
    if end < 0:
        return None
    return text[4:end], text[end + 5 :]


def _iter_wiki_files(wiki_root: Path):
    """Yield top-level ``*.md`` files, skipping ``_*`` (same as cwc scripts)."""
    for path in sorted(wiki_root.glob("*.md")):
        if path.name.startswith("_"):
            continue
        yield path


def _atomic_write(path: Path, content: str) -> None:
    """Write ``content`` to ``path`` atomically via temp-file + ``os.replace``.

    Avoids leaving a partial file on disk if the process is interrupted
    mid-write — readers see either the old content or the complete new
    content, never a truncated mix.
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# 1. Tag-indent normalization


def _normalize_block_lists(fm: str) -> tuple[str, bool]:
    """Force every top-level scalar block-list to use 2-space dash indent.

    Skips blocks where any item has a continuation line (i.e. it's a
    map-list, not a scalar list) — those need different handling.
    """
    lines = fm.split("\n")
    out: list[str] = []
    i = 0
    changed = False
    while i < len(lines):
        line = lines[i]
        if (
            line
            and not line.startswith(" ")
            and line.endswith(":")
            and ":" in line
            and " " not in line.rstrip(":")
        ):
            j = i + 1
            block_lines: list[str] = []
            has_continuation = False
            local_changed = False
            while j < len(lines):
                nxt = lines[j]
                if not nxt:
                    break
                stripped = nxt.lstrip(" ")
                indent = len(nxt) - len(stripped)
                if stripped.startswith("- ") or stripped == "-":
                    if indent != 2:
                        local_changed = True
                    block_lines.append("  " + stripped)
                    j += 1
                    continue
                if indent > 0 and not stripped.startswith("-"):
                    has_continuation = True
                    break
                break
            if block_lines and not has_continuation:
                out.append(line)
                out.extend(block_lines)
                if local_changed:
                    changed = True
                i = j
                continue
        out.append(line)
        i += 1
    return "\n".join(out), changed


def repair_tag_indent(wiki_root: Path, apply: bool = False) -> RepairReport:
    """Normalize tag-list (and other top-level block-list) indentation.

    See module docstring for the corruption shape. Idempotent: files
    already at 2-space indent are left untouched.
    """
    report = RepairReport()
    if not wiki_root.is_dir():
        report.errors.append((wiki_root, "wiki root not found"))
        return report

    for path in _iter_wiki_files(wiki_root):
        report.files_scanned += 1
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            report.errors.append((path, f"read_error: {exc}"))
            continue
        parts = _split_frontmatter(text)
        if parts is None:
            continue
        fm, body = parts
        new_fm, changed = _normalize_block_lists(fm)
        if not changed:
            continue
        # Validate the rewrite parses before recording / writing
        try:
            meta = yaml.safe_load(new_fm)
        except yaml.YAMLError as exc:
            report.errors.append((path, f"still_broken: {str(exc)[:80]}"))
            continue
        if not isinstance(meta, dict):
            report.errors.append((path, "not_dict_after_parse"))
            continue
        report.files_changed += 1
        report.changes.append((path, "tag-indent normalized to 2-space"))
        if apply:
            try:
                _atomic_write(path, "---\n" + new_fm + "\n---\n" + body)
            except OSError as exc:
                report.errors.append((path, f"write_error: {exc}"))

    return report


# ---------------------------------------------------------------------------
# 2. Value-quoting repair

# Match keys (with optional dash prefix for list items) where value either:
#  (a) starts with `[` (looks like flow-sequence to YAML)
#  (b) is a bare `-` (treated as block-sequence start)
#  (c) starts with `- ` followed by content
_VALUE_QUOTING_RE = re.compile(
    r"^(\s*-?\s*)(title|organization_name|apollo_headline|current_title|current_company): "
    r"(\[[^\n]*\][^\n]*|- ?[^\n]*)$",
    re.MULTILINE,
)


def _yaml_parses(fm: str) -> bool:
    try:
        yaml.safe_load(fm)
        return True
    except yaml.YAMLError:
        return False


def _quote_subst(m: re.Match) -> str:
    indent, key, val = m.group(1), m.group(2), m.group(3)
    if val.strip() in {"-", ""}:
        return f"{indent}{key}: ''"
    esc = val.replace("\\", "\\\\").replace('"', '\\"')
    return f'{indent}{key}: "{esc}"'


def repair_value_quoting(wiki_root: Path, apply: bool = False) -> RepairReport:
    """Quote unquoted YAML values that break ``safe_load``.

    Only rewrites a file if the original frontmatter fails to parse AND
    the rewrite parses cleanly. Idempotent on already-clean trees.
    """
    report = RepairReport()
    if not wiki_root.is_dir():
        report.errors.append((wiki_root, "wiki root not found"))
        return report

    for path in _iter_wiki_files(wiki_root):
        report.files_scanned += 1
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            report.errors.append((path, f"read_error: {exc}"))
            continue
        parts = _split_frontmatter(text)
        if parts is None:
            continue
        fm, body = parts
        if _yaml_parses(fm):
            continue
        new_fm = _VALUE_QUOTING_RE.sub(_quote_subst, fm)
        if new_fm == fm:
            continue
        if not _yaml_parses(new_fm):
            continue
        report.files_changed += 1
        report.changes.append((path, "value-quoting repaired"))
        if apply:
            try:
                _atomic_write(path, "---\n" + new_fm + "\n---\n" + body)
            except OSError as exc:
                report.errors.append((path, f"write_error: {exc}"))

    return report
