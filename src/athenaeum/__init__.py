# SPDX-License-Identifier: Apache-2.0
"""Athenaeum — open source knowledge management pipeline.

The Python API surface (``__all__``) is intentionally narrow and stable.
Most new functionality in this release is delivered via CLI subcommands
(``athenaeum people``, ``athenaeum recall``, ``athenaeum repair``,
``athenaeum dedupe``, ``athenaeum questions``, ``athenaeum ingest-answers``,
etc.) — see the CLI documentation in ``README.md`` and ``athenaeum --help``
for the full subcommand list. Internal modules (``contradictions``,
``merge``, ``clusters``, ``dedupe``, ``repair``, ``answers``,
``provenance``, ``resolutions``) are importable but not part of the stable
public surface; their signatures may change between minor releases.
"""

__version__ = "0.4.0"

from athenaeum.init import init_knowledge_dir
from athenaeum.librarian import discover_raw_files, process_one, rebuild_index, run
from athenaeum.models import (
    ClassifiedEntity,
    EntityAction,
    EntityIndex,
    EscalationItem,
    ProcessingResult,
    RawFile,
    WikiEntity,
    generate_uid,
    parse_frontmatter,
    render_frontmatter,
    slugify,
)

__all__ = [
    "ClassifiedEntity",
    "EntityAction",
    "EntityIndex",
    "EscalationItem",
    "ProcessingResult",
    "RawFile",
    "WikiEntity",
    "discover_raw_files",
    "generate_uid",
    "init_knowledge_dir",
    "parse_frontmatter",
    "process_one",
    "rebuild_index",
    "render_frontmatter",
    "run",
    "slugify",
]
