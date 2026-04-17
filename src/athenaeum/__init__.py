# SPDX-License-Identifier: Apache-2.0
"""Athenaeum — open source knowledge management pipeline."""

__version__ = "0.2.1"

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
