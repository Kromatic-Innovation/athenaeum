# SPDX-License-Identifier: Apache-2.0
"""Shared idempotent-append helper for ``docs/measurements/memory-model-measurements.md``.

Issue athenaeum#713 (v6 MVP (c), the measurement pack) needs the SAME idempotent-
append shape :func:`athenaeum.push_metrics.write_snapshot` already established
for that file (issue athenaeum#711): file absent -> write the shared header plus one
new section; section absent -> append the section at EOF; section present ->
insert just the new dated sub-entry right after the existing heading line so
repeated runs accumulate entries in run order and NEVER overwrite an earlier
one. Three new artifact modules (shadow-linkage, backlog price sheet,
ordinary-night table) all need this exact behaviour against different
section headings in the SAME file, so it is factored here once rather than
copied three times — the same "one place, not N" discipline
:mod:`athenaeum.clusters` documents for its own C1-C4 formation routine and
:mod:`athenaeum.wiki_dedupe` echoes for issue athenaeum#803.

``push_metrics.write_snapshot`` is intentionally left as-is (it predates this
module and is already shipped/tested) — this is not a refactor of that
function, just the shared primitive its three new siblings build on.

**Reading pinned by issue athenaeum#1095 (AC6):** "replaces" in the AC's
"replaces its own section, not the whole document" means each generator
replaces/updates only its OWN ``##`` section — it ACCUMULATES dated
sub-entries within that section and never overwrites an earlier one, and it
never touches a sibling ``##`` section's heading or entries. A destructive
whole-document or whole-section overwrite would discard measurement history,
which the AC's own document-scope-vs-section-scope framing rules out.

Layering: L2 utility. Imports only :mod:`athenaeum.atomic_io`.
"""

from __future__ import annotations

from pathlib import Path

from athenaeum.atomic_io import atomic_write_text

#: Identical preamble to ``push_metrics._SNAPSHOT_HEADER`` — this IS the file
#: header, not a per-artifact one, so every writer that creates the file from
#: scratch must render the same text regardless of which artifact ran first.
DOCS_HEADER = """# Memory model measurements

Durable home for v6 (dimensional memory model) measurement artifacts.
Each `##` section is produced by one epic child issue and states, inline,
the reproducible command that generated it. This file is committed —
`docs/memory-model.md` (the design lock) is never touched by any command
that writes here.
"""


def append_measurement_section(
    docs_path: Path,
    *,
    section_heading: str,
    entry_markdown: str,
) -> Path:
    """Idempotently write/append one dated *entry_markdown* under *section_heading*.

    Args:
        docs_path: ``docs/measurements/memory-model-measurements.md`` (or a test-owned
            stand-in).
        section_heading: e.g. ``"## Shadow-mode complete-linkage population"``
            — matched as a literal substring, exactly as
            ``push_metrics.write_snapshot`` matches its own heading.
        entry_markdown: the dated sub-entry body (starting with ``### Snapshot
            ...`` or equivalent) — WITHOUT the ``##`` section heading line.

    Behaviour:

    - File absent: write :data:`DOCS_HEADER` + *section_heading* + *entry_markdown*.
    - File present, *section_heading* absent: append *section_heading* +
      *entry_markdown* at EOF.
    - File present, *section_heading* present: insert *entry_markdown*
      immediately after the heading line, so entries accumulate in run
      order and an earlier entry is never touched or reordered.

    Uses :func:`athenaeum.atomic_io.atomic_write_text` for the whole-file
    replace, so a crash mid-write can never leave a torn file.
    """
    entry = entry_markdown.strip("\n")

    if not docs_path.is_file():
        content = DOCS_HEADER + "\n" + section_heading + "\n\n" + entry + "\n"
        atomic_write_text(docs_path, content)
        return docs_path

    existing = docs_path.read_text(encoding="utf-8")
    if section_heading not in existing:
        sep = "" if existing.endswith("\n\n") else ("\n" if existing.endswith("\n") else "\n\n")
        content = existing + sep + section_heading + "\n\n" + entry + "\n"
        atomic_write_text(docs_path, content)
        return docs_path

    heading_idx = existing.index(section_heading)
    insert_at = heading_idx + len(section_heading)
    content = existing[:insert_at] + "\n\n" + entry + "\n" + existing[insert_at:]
    atomic_write_text(docs_path, content)
    return docs_path
