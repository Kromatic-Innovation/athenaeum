# SPDX-License-Identifier: Apache-2.0
"""Deterministic field-correction fast path (issue athenaeum#797).

Implements the conformance format specified in `docs/field-corrections.md`:
a `.jsonl` batch a writer MAY submit into the ordinary `raw/<source>/` intake
tree to have a field-level change applied at tier 0 (mechanical, no LLM),
instead of paying LLM compilation per fact or bypassing the librarian
entirely.

**The one rule this module must preserve** (`field-corrections.md` §1.1):
conformance sets how deep in the tier ladder a submission enters; it never
sets whether it enters. Every failure to conform is a fallthrough to a
higher tier, never a rejection — a `None`/empty return from any function
here means "the caller must route this to ordinary intake / reasoning",
never "drop it."

Layering: L2 primitive, same tier as :mod:`athenaeum.intake`. Imports only
leaf/service modules — :mod:`athenaeum.models`, :mod:`athenaeum.provenance`,
:mod:`athenaeum.precedence`, :mod:`athenaeum.registry`,
:mod:`athenaeum.schemas`, :mod:`athenaeum.atomic_io`, :mod:`athenaeum.config`.
Must never import :mod:`athenaeum.intake`, :mod:`athenaeum.librarian`,
:mod:`athenaeum.merge`, or :mod:`athenaeum.tiers` — `intake.py` imports
:func:`parse_batch_envelope` from here (the "valid envelope" single
definition, §3.1), and a back-edge would reintroduce the import cycle those
modules were split to avoid (issue athenaeum#545).

This module is organized in the order `docs/field-corrections.md` presents
its sections:

- §3.1 valid-envelope recognition (:func:`parse_batch_envelope`) — the
  single definition shared by `intake.discover_raw_files`'s skip and this
  module's own batch processing, so the two can never drift (§3.1's own
  warning: a schema_version check dropped from one site but not the other
  reintroduces the "seen by nothing" bug this design exists to remove).
"""

from __future__ import annotations

import json
from typing import Any

#: `schema_version` values this build knows how to process. A batch
#: declaring any other value is deliberately NOT a valid envelope
#: (`docs/field-corrections.md` §3.1 condition 3) — it is left as ordinary
#: intake rather than being skipped by discovery and then found
#: un-processable by the correction phase, which is exactly the silent-drop
#: bug §3.1 calls out by name.
KNOWN_SCHEMA_VERSIONS: frozenset[int] = frozenset({1})


def parse_batch_envelope(first_line: str) -> dict[str, Any] | None:
    """Parse a `.jsonl` file's first line as a correction-batch envelope.

    Returns the parsed envelope dict when ALL of
    `docs/field-corrections.md` §3.1's conditions hold:

    1. It parses as JSON.
    2. ``record == "batch"``.
    3. ``schema_version`` is present and is a version this build knows how
       to process (:data:`KNOWN_SCHEMA_VERSIONS`).
    4. ``batch_id`` and ``created_at`` are present (non-empty).

    Returns ``None`` otherwise — deliberately not an exception. This is THE
    single definition of "valid envelope," used by both
    `intake.discover_raw_files`'s skip (so a conformant batch is claimed by
    the correction phase instead of being double-processed as prose) and by
    the correction phase itself. A line that fails this check is ordinary
    raw intake — nothing here rejects a batch or a file; only discovery's
    caller decides where a non-envelope line ends up (ordinary intake,
    unchanged).
    """
    try:
        obj = json.loads(first_line)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(obj, dict):
        return None
    if obj.get("record") != "batch":
        return None
    schema_version = obj.get("schema_version")
    # bool is an int subclass; exclude it explicitly (matches the
    # bool-is-an-int-subclass guard convention in athenaeum.config).
    if not isinstance(schema_version, int) or isinstance(schema_version, bool):
        return None
    if schema_version not in KNOWN_SCHEMA_VERSIONS:
        return None
    if not obj.get("batch_id") or not obj.get("created_at"):
        return None
    return obj
