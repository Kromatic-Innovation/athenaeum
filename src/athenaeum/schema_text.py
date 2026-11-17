# SPDX-License-Identifier: Apache-2.0
"""Bounded, defanged reader for operator-tunable schema fragments — L3 service.

Contract: reads a fragment from ``wiki/_schema/`` for interpolation into a
prompt's instruction position (issue athenaeum#17, hardened by
athenaeum#563). Shared by the global fragments in :mod:`athenaeum.tiers` and
the per-type fragments in :mod:`athenaeum.type_guidance`; it lives in its own
leaf module so those two do not import each other (issue athenaeum#2731).
Both names remain importable from ``athenaeum.tiers``.
"""

from __future__ import annotations

import logging
from pathlib import Path

from athenaeum.prompt_safety import defang_tag

log = logging.getLogger(__name__)

# Cap on operator-tunable schema fragments read from the live wiki at runtime
# (issue athenaeum#563). Both shipped defaults are well under 2KB; the cap degrades a
# too-long or accidentally-appended fragment (truncate, do not drop) so it
# cannot silently inflate every classify/create call.
_SCHEMA_FRAGMENT_MAX_CHARS = 8192


def _load_schema_text(wiki_root: Path, filename: str) -> str:
    """Load an operator-tunable schema fragment from the live wiki.

    This is the designed operator tuning knob (issue athenaeum#17): the package ships
    defaults under ``athenaeum.schema`` that ``init`` copies write-once into
    ``wiki/_schema/``, and this reads the (possibly edited) copy back at runtime.
    Returns ``''`` if the fragment is absent or unreadable.

    Hardened per issue athenaeum#563, because the two callers interpolate the result into
    prompt *instruction position* next to the fenced ``<user_document>`` block:

    - **Bounded** to :data:`_SCHEMA_FRAGMENT_MAX_CHARS` — a too-long fragment is
      truncated (with a warning naming the file and its size), never dropped.
    - **Defanged** — literal ``<user_document>`` / ``</user_document>`` markers
      are neutralized so an edited fragment cannot forge the trust boundary of
      the adjacent untrusted block.

    A fragment with no fence markers that is under the cap (both shipped
    defaults) renders byte-identically to before.
    """
    path = wiki_root / "_schema" / filename
    if not path.exists():
        return ""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    if len(text) > _SCHEMA_FRAGMENT_MAX_CHARS:
        log.warning(
            "schema fragment %s is %d chars; truncating to %d (issue athenaeum#563)",
            filename,
            len(text),
            _SCHEMA_FRAGMENT_MAX_CHARS,
        )
        text = text[:_SCHEMA_FRAGMENT_MAX_CHARS]
    return defang_tag(text, "user_document")
