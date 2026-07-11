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

from athenaeum.atomic_io import atomic_write_text


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


# ---------------------------------------------------------------------------
# 3. Legacy bare-slug ``source:`` migration  (issue #97 / design-lock §5)

# Fixed mapping from the live-tree inventory in
# ``docs/provenance-shape.md`` §5.1. Both observed bare-slug values are
# librarian scripts — ``script:<slug>`` is the typed form. Unknown slugs
# ABORT the migration (design-lock §5.2 — no guess, no fallback).
LEGACY_SLUG_MAP: dict[str, str] = {
    "extended-tier-build": "script:extended-tier-build",
    "warm-network-detect": "script:warm-network-detect",
}

# Match a top-level ``source:`` line whose value is a bare slug
# (``[a-z][a-z0-9_-]*``, no colon — i.e. NOT already typed). Anchored to
# start-of-line so we never touch nested mapping keys.
_SOURCE_LINE_RE = re.compile(
    r"^source:[ \t]+([a-z][a-z0-9_-]*)[ \t]*$",
    re.MULTILINE,
)


@dataclass
class LegacySlugReport:
    """Result of a legacy-slug migration scan/apply pass.

    Attributes:
        files_scanned: total ``.md`` files inspected.
        would_rewrite: dry-run count of wikis that WOULD be migrated.
        rewrites_applied: apply-mode count of wikis ACTUALLY migrated.
        skipped_validation_fail: rewrites whose new typed ``source:``
            value failed :func:`provenance.parse_source` and were
            skipped. NOTE: name retained for CLI output stability; the
            check now validates ONLY the rewritten source line, not the
            full wiki frontmatter (see PR fix/migration-relax-safeguard).
        unknown_slugs: per-slug count of bare-slug values not present in
            :data:`LEGACY_SLUG_MAP`. Non-empty → migration ABORTED.
        unknown_slug_files: first 10 ``(path, slug)`` pairs for unknown
            slugs (for the error report).
        per_slug_counts: per-known-slug count of wikis affected.
        changes: ``(path, summary)`` per migrated wiki.
        errors: ``(path, error-message)`` for read/write failures.
    """

    files_scanned: int = 0
    would_rewrite: int = 0
    rewrites_applied: int = 0
    skipped_validation_fail: int = 0
    unknown_slugs: dict[str, int] = field(default_factory=dict)
    unknown_slug_files: list[tuple[Path, str]] = field(default_factory=list)
    per_slug_counts: dict[str, int] = field(default_factory=dict)
    changes: list[tuple[Path, str]] = field(default_factory=list)
    errors: list[tuple[Path, str]] = field(default_factory=list)


def migrate_legacy_source_slugs(
    wiki_root: Path, *, apply: bool = False
) -> LegacySlugReport:
    """Migrate legacy bare-slug ``source:`` values to typed ``script:<slug>``.

    Walks ``wiki_root`` looking for a top-level ``source:`` line whose
    value matches the bare-slug shape (``[a-z][a-z0-9_-]*``, no colon).
    Each match is looked up in :data:`LEGACY_SLUG_MAP`:

    - Known slug → rewrite the ``source:`` line in place to the typed
      form. ONLY that line is touched; the rest of the file (frontmatter
      and body) is byte-for-byte preserved.
    - Unknown slug → collected into ``unknown_slugs`` and the migration
      is ABORTED before any rewrite is written. Per design-lock §5.2,
      this is deliberate — no guess, no partial apply.

    In ``apply`` mode, every rewrite's NEW typed source value is parsed
    via :func:`athenaeum.provenance.parse_source` BEFORE the file is
    written. Failures are recorded under ``skipped_validation_fail``
    and the original file is left intact. The check is intentionally
    narrow: only the source line changes, so only the source line is
    validated. Pre-existing frontmatter issues (e.g. unrelated invalid
    fields) do NOT block the trivial source rewrite.

    Idempotent: a typed ``source: script:extended-tier-build`` does NOT
    match :data:`_SOURCE_LINE_RE` (the regex rejects values containing a
    colon), so a re-run on a fully migrated tree finds zero candidates.
    """
    # Local import — avoids a top-level cycle between repair and provenance.
    from athenaeum.provenance import parse_source

    report = LegacySlugReport()
    if not wiki_root.is_dir():
        report.errors.append((wiki_root, "wiki root not found"))
        return report

    # Phase 1: scan every file, classify each match. We collect ALL
    # unknown slugs across the whole tree before deciding whether to
    # apply — partial-apply on an unknown-slug tree would corrupt data.
    candidates: list[tuple[Path, str, str, str, str]] = []
    # entries: (path, original_text, new_text, slug, typed_form)

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
        fm, _body = parts
        m = _SOURCE_LINE_RE.search(fm)
        if not m:
            continue
        slug = m.group(1)
        if slug not in LEGACY_SLUG_MAP:
            report.unknown_slugs[slug] = report.unknown_slugs.get(slug, 0) + 1
            if len(report.unknown_slug_files) < 10:
                report.unknown_slug_files.append((path, slug))
            continue
        typed = LEGACY_SLUG_MAP[slug]
        # Build new frontmatter by replacing ONLY the matched source
        # line. We preserve indentation/whitespace by reusing the
        # pre-value prefix and stripping any trailing whitespace.
        new_fm = fm[: m.start()] + f"source: {typed}" + fm[m.end() :]
        new_text = "---\n" + new_fm + "\n---\n" + parts[1]
        candidates.append((path, text, new_text, slug, typed))

    # Phase 2: if any unknown slugs were seen, ABORT. No rewrites.
    if report.unknown_slugs:
        return report

    # Phase 3: account counts (dry-run reports `would_rewrite`).
    for _path, _orig, _new, slug, _typed in candidates:
        report.per_slug_counts[slug] = report.per_slug_counts.get(slug, 0) + 1
    report.would_rewrite = len(candidates)

    if not apply:
        for path, _orig, _new, slug, typed in candidates:
            report.changes.append((path, f"{slug} -> {typed}"))
        return report

    # Phase 4: apply. Validate ONLY the new typed source value via
    # parse_source — the source line is the only thing this migration
    # changes, so it is the only thing we should gate on. Pre-existing
    # frontmatter issues (e.g. integer uid, malformed unrelated fields)
    # are out of scope for this migration and must NOT block the
    # trivial source rewrite. Skip (don't abort) on per-file failure.
    for path, _orig, new_text, slug, typed in candidates:
        try:
            parsed = parse_source(typed)
            if parsed is None:
                raise ValueError("parse_source returned None")
        except Exception as exc:  # noqa: BLE001 — defensive guard
            report.skipped_validation_fail += 1
            report.errors.append((path, f"invalid_source: {str(exc)[:120]}"))
            continue
        try:
            _atomic_write(path, new_text)
        except OSError as exc:
            report.errors.append((path, f"write_error: {exc}"))
            continue
        report.rewrites_applied += 1
        report.changes.append((path, f"{slug} -> {typed}"))

    return report


# ---------------------------------------------------------------------------
# 4. Source backfill — re-classify DEFAULTED ``claude:inferred`` (issue #328)

# The scalar that :func:`athenaeum.mcp_server.remember` stamps when a caller
# supplies no ``sources`` (mirrors ``mcp_server._DEFAULT_INFERRED_SOURCE``).
# Only memories carrying EXACTLY this defaulted scalar are candidates — a
# caller-declared source is never second-guessed.
_DEFAULTED_INFERRED_SOURCE = "claude:inferred"

# Idempotency marker written on the confirm-inferred path (§3 of the #328
# lock). A boolean; presence means "already re-examined, no support found" so
# the pass skips the memory on every subsequent run WITHOUT changing precedence.
_INFERRED_VERIFIED_KEY = "inferred_verified"


@dataclass
class BackfillReport:
    """Result of a ``repair --backfill-sources`` scan/apply pass (issue #328).

    Attributes:
        files_scanned: total ``*.md`` intake files inspected.
        user_stated: memories upgraded to ``user-stated`` (source scalar
            rewritten to ``user:<ref>``; resolver tier 1).
        agent_observed: memories upgraded to ``agent-observed`` (source scalar
            rewritten to ``agent-observed:<model>:<ref>``).
        confirmed_inferred: memories confirmed ``inferred`` (idempotency
            marker ``inferred_verified: true`` stamped; precedence unchanged).
        changes: ``(path, summary)`` per proposed/applied upgrade.
        skips: ``(path, reason)`` for candidates deliberately not touched
            (missing origin session, transcript unavailable, ...).
        errors: ``(path, error-message)`` for read/parse/write failures.
        resume_after: when a ``limit`` cut the batch short, the scope-relative
            path of the last memory acted on — a follow-up run continues past
            it (idempotency makes the resume implicit).
    """

    files_scanned: int = 0
    user_stated: int = 0
    agent_observed: int = 0
    confirmed_inferred: int = 0
    changes: list[tuple[Path, str]] = field(default_factory=list)
    skips: list[tuple[Path, str]] = field(default_factory=list)
    errors: list[tuple[Path, str]] = field(default_factory=list)
    resume_after: str | None = None


def _iter_auto_memory_files(auto_memory_root: Path):
    """Yield ``<auto_memory_root>/<scope>/*.md`` intake files, skipping ``_*``.

    Auto-memory is scope-indexed (``raw/auto-memory/<scope>/<name>.md``), so the
    parent directory name IS the origin scope (and the transcript scope). Files
    beginning with ``_`` (sidecars) are skipped, matching the wiki convention.
    """
    for path in sorted(auto_memory_root.glob("*/*.md")):
        if path.name.startswith("_"):
            continue
        yield path


def _yaml_scalar_literal(value: str) -> str:
    """Render *value* as a YAML scalar RHS, single-quoting when unsafe as plain.

    Source scalars (``user:...``, ``agent-observed:...``) and refs
    (``<session>#turn<N>``) carry ``:`` / ``#`` that YAML would otherwise
    misparse, so they are single-quoted (with ``'`` doubled). Bare model ids
    stay plain.
    """
    special = set(":#")
    if (
        value == ""
        or value != value.strip()
        or any(c in special for c in value)
        or value[0] in "!&*[]{}>|@`\"'%,"
    ):
        return "'" + value.replace("'", "''") + "'"
    return value


def _set_frontmatter_scalar(fm: str, key: str, value_literal: str) -> str:
    """Replace the top-level ``key:`` line's value, or append the key.

    Only the single ``key:`` line is rewritten; every other line is preserved
    byte-for-byte (the ``repair --legacy-source-slugs`` discipline). ``fm`` is
    the frontmatter body WITHOUT the ``---`` fences and without a trailing
    newline (as returned by :func:`_split_frontmatter`). The anchored
    ``^key:`` match never touches an indented (nested) key of the same name.
    """
    pattern = re.compile(rf"^{re.escape(key)}:.*$", re.MULTILINE)
    replacement = f"{key}: {value_literal}"
    new_fm, n = pattern.subn(lambda _m: replacement, fm, count=1)
    if n:
        return new_fm
    sep = "\n" if fm else ""
    return fm + sep + replacement


def _extract_claim(meta: dict, body: str) -> str:
    """Return the claim text to match: ``name``/``title``, else first body line.

    The #328 lock fixes THE CLAIM = title/name, falling back to the first
    non-frontmatter line when neither frontmatter key is present.
    """
    for key in ("name", "title"):
        val = meta.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    for line in body.splitlines():
        if line.strip():
            return line.strip()
    return ""


def backfill_sources(
    auto_memory_root: Path,
    *,
    projects_root: Path | None = None,
    apply: bool = False,
    asserter: dict[str, object] | None = None,
    limit: int | None = None,
) -> BackfillReport:
    """Re-classify DEFAULTED ``claude:inferred`` memories against transcripts.

    For each auto-memory whose ``source:`` scalar is EXACTLY the defaulted
    ``claude:inferred`` (and which has not already been confirmed via
    ``inferred_verified``), locate the origin transcript via ``originSessionId``
    (+ ``originTurn``) in the scope-indexed directory and match THE CLAIM
    (``name``/``title``, fallback first body line) as a normalized substring:

    1. **User said it → ``user-stated``.** Rewrite the ``source:`` scalar to
       ``user:<session>#turn<N>`` (resolver tier 1) and set
       ``source_type: user-stated`` / ``source_ref``. ``on_behalf_of`` is
       populated from *asserter* ONLY when it yields a durable identity key
       (transcripts carry no OIDC identity, so this is usually absent).
    2. **Derived from a tool-result artifact → ``agent-observed``.** Rewrite the
       scalar to ``agent-observed:<model>:<session-ref>`` (``model`` omitted
       when the transcript carries none) and set
       ``source_type: agent-observed`` / ``source_ref`` / ``model``.
    3. **No support found → confirm inferred.** Stamp
       ``inferred_verified: true`` (idempotency marker; precedence unchanged).

    Only provenance keys are touched — the body and all other frontmatter lines
    are preserved byte-for-byte. Idempotent: an already-upgraded memory (scalar
    no longer ``claude:inferred``) or a confirmed one (``inferred_verified``)
    is skipped. A missing/rolled-off transcript is SKIPPED with a logged reason
    (never guessed). ``dry-run`` (``apply=False``) records proposals and writes
    nothing. ``limit`` bounds the number of memories acted on per run for a
    resumable batch.
    """
    from athenaeum.models import asserter_identity_key
    from athenaeum.provenance import parse_source
    from athenaeum.transcript_verify import classify_backfill_claim

    report = BackfillReport()
    if not auto_memory_root.is_dir():
        report.errors.append((auto_memory_root, "auto-memory root not found"))
        return report

    # Owner asserter → on_behalf_of principal name, but ONLY when it carries a
    # durable OIDC identity key (§10 discipline: no key ⇒ no identity claim).
    on_behalf_of = ""
    if asserter and asserter_identity_key(asserter):
        name = asserter.get("name")
        if isinstance(name, str) and name.strip():
            on_behalf_of = name.strip()

    acted = 0
    for path in _iter_auto_memory_files(auto_memory_root):
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
        try:
            meta = yaml.safe_load(fm)
        except yaml.YAMLError as exc:
            report.errors.append((path, f"unparseable_frontmatter: {str(exc)[:80]}"))
            continue
        if not isinstance(meta, dict):
            continue

        # Candidate gate + idempotency: only the DEFAULTED scalar, and never a
        # memory already confirmed inferred.
        if meta.get("source") != _DEFAULTED_INFERRED_SOURCE:
            continue
        if meta.get(_INFERRED_VERIFIED_KEY) is True:
            continue

        session_id = str(meta.get("originSessionId") or "").strip()
        if not session_id:
            report.skips.append((path, "no-origin-session"))
            continue
        turn_raw = meta.get("originTurn")
        try:
            turn = int(turn_raw) if turn_raw is not None else None
        except (TypeError, ValueError):
            turn = None
        scope = path.parent.name
        claim = _extract_claim(meta, body)

        result = classify_backfill_claim(
            scope, session_id, turn=turn, claim=claim, projects_root=projects_root
        )
        if result.channel == "unavailable":
            report.skips.append((path, "transcript-unavailable"))
            continue

        # Build the surgical frontmatter edit for this channel.
        new_source: str | None
        if result.channel == "user-stated":
            new_source = f"user:{result.ref}"
            new_fm = _set_frontmatter_scalar(
                fm, "source", _yaml_scalar_literal(new_source)
            )
            new_fm = _set_frontmatter_scalar(new_fm, "source_type", "user-stated")
            new_fm = _set_frontmatter_scalar(
                new_fm, "source_ref", _yaml_scalar_literal(result.ref)
            )
            if on_behalf_of:
                new_fm = _set_frontmatter_scalar(
                    new_fm, "on_behalf_of", _yaml_scalar_literal(on_behalf_of)
                )
            summary = f"user-stated: source -> {new_source}"
        elif result.channel == "agent-observed":
            if result.model:
                new_source = f"agent-observed:{result.model}:{result.ref}"
            else:
                new_source = f"agent-observed:{result.ref}"
            new_fm = _set_frontmatter_scalar(
                fm, "source", _yaml_scalar_literal(new_source)
            )
            new_fm = _set_frontmatter_scalar(new_fm, "source_type", "agent-observed")
            new_fm = _set_frontmatter_scalar(
                new_fm, "source_ref", _yaml_scalar_literal(result.ref)
            )
            if result.model:
                new_fm = _set_frontmatter_scalar(
                    new_fm, "model", _yaml_scalar_literal(result.model)
                )
            summary = f"agent-observed: source -> {new_source}"
        else:  # confirm inferred
            new_source = None
            new_fm = _set_frontmatter_scalar(fm, _INFERRED_VERIFIED_KEY, "true")
            summary = "confirm-inferred: inferred_verified: true"

        # Validate the rewrite parses (and the new source scalar is legal)
        # before recording/writing — mirror the legacy-slug safeguard.
        try:
            reparsed = yaml.safe_load(new_fm)
            if not isinstance(reparsed, dict):
                raise ValueError("frontmatter no longer a mapping")
            if new_source is not None and parse_source(new_source) is None:
                raise ValueError("rewritten source failed to parse")
        except (yaml.YAMLError, ValueError) as exc:
            report.errors.append((path, f"validation_failed: {str(exc)[:80]}"))
            continue

        if result.channel == "user-stated":
            report.user_stated += 1
        elif result.channel == "agent-observed":
            report.agent_observed += 1
        else:
            report.confirmed_inferred += 1
        report.changes.append((path, summary))

        if apply:
            try:
                atomic_write_text(path, "---\n" + new_fm + "\n---\n" + body)
            except OSError as exc:
                report.errors.append((path, f"write_error: {exc}"))
                continue

        acted += 1
        if limit is not None and acted >= limit:
            report.resume_after = f"{scope}/{path.name}"
            break

    return report


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
