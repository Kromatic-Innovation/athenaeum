# SPDX-License-Identifier: Apache-2.0
"""Athenaeum — open source knowledge management pipeline.

Contract: this is the package root; its ``__all__`` is the ONLY import
surface covered by semver compatibility guarantees. Everything exported here
is a thin re-export (init, top-level librarian entry points, and the handful
of :mod:`athenaeum.models` shapes/helpers callers need) — no logic lives in
this file itself.

Factoring rule: adding a name to ``__all__`` is a public-API commitment: do it
only for something a library consumer (not the CLI) genuinely needs, and keep
the list in sync with the imports above it. Most functionality is exercised
through the CLI subcommands (``athenaeum people``, ``athenaeum recall``,
``athenaeum repair``, ``athenaeum dedupe``, ``athenaeum questions``,
``athenaeum ingest-answers``, ``athenaeum ingest-merges``, etc.) — see
``README.md`` and ``athenaeum --help``. Internal modules (``contradictions``,
``merge``, ``clusters``, ``delta``, ``dedupe``, ``repair``, ``answers``,
``provenance``, ``resolutions``, ``json_utils``, ``retire``, ``owner``,
``transcript_verify``, ``wiki_dedupe``) are importable but not part of the
stable public surface; their signatures may change between minor releases.

Layering: sits above L5 by necessity (it imports the CLI-adjacent
:mod:`athenaeum.librarian` pipeline entry points as well as the L1
:mod:`athenaeum.models` hub and L1 :mod:`athenaeum.init`) — this file is the
one place layering is deliberately collapsed, since a package root must expose
the whole stack's public surface in one namespace.
"""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

try:
    # Single-sourced from the installed distribution's metadata (issue athenaeum#555,
    # L12) -- hatchling populates this from pyproject.toml's `version` at
    # build time, so this constant and pyproject.toml can never drift apart
    # the way a hand-duplicated literal here could (and once did: athenaeum#555).
    __version__ = _pkg_version("athenaeum")
except PackageNotFoundError:
    # Editable/source checkout with no installed dist metadata (e.g. running
    # straight from a git clone without `pip install`). Not expected in this
    # repo's supported workflows (the dev extra always installs the
    # package), but fail soft rather than raising at import time.
    __version__ = "0.0.0+unknown"

from athenaeum.init import init_knowledge_dir
from athenaeum.librarian import (
    IngestResult,
    SessionEndResult,
    discover_raw_files,
    ingest,
    process_one,
    rebuild_index,
    reindex,
    run,
    session_end,
)
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
    "IngestResult",
    "ProcessingResult",
    "RawFile",
    "SessionEndResult",
    "WikiEntity",
    "discover_raw_files",
    "generate_uid",
    "ingest",
    "init_knowledge_dir",
    "parse_frontmatter",
    "process_one",
    "rebuild_index",
    "reindex",
    "render_frontmatter",
    "run",
    "session_end",
    "slugify",
]
