# SPDX-License-Identifier: Apache-2.0
"""Shared ``wiki/auto-*.md`` page discovery (issue athenaeum#278).

The one-shot prune driver that used to live here (issue athenaeum#278, Part 2
-- the ``athenaeum auto-memory prune`` CLI and its ``build_prune_report`` /
``apply_prune`` pair) was removed once its underlying intake-side defect was
fixed (athenaeum#2466): the ephemeral-intake classifier now stops NEW
operational session notes from becoming durable wiki entities at the source,
so the retroactive sweep over already-compiled pages is no longer needed.

What remains is the shared discovery primitive
(:func:`discover_auto_pages`) and the ``type: auto-memory`` constant
(:data:`AUTO_MEMORY_TYPE`), both still consumed by
:mod:`athenaeum._cmd_recovery_yield` to enumerate compiled auto-memory pages
for yield accounting -- unrelated to the removed prune sweep.

Layering: L4 domain/pipeline module.
"""

from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)

# Compiled merged auto-memory pages are named ``auto-<topic-slug>.md`` (mirrors
# :data:`athenaeum.merge.AUTO_WIKI_PREFIX`). Re-declared here so callers do
# not need to import the whole merge module just for one constant.
AUTO_WIKI_PREFIX = "auto-"

AUTO_MEMORY_TYPE = "auto-memory"


def discover_auto_pages(wiki_root: Path) -> list[Path]:
    """Return sorted ``wiki/auto-*.md`` files (shallow; skips subdirs)."""
    if not wiki_root.is_dir():
        return []
    return sorted(p for p in wiki_root.glob(f"{AUTO_WIKI_PREFIX}*.md") if p.is_file())
