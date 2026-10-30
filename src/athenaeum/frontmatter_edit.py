# SPDX-License-Identifier: Apache-2.0
"""Surgical single-key frontmatter edits, and the one witnessed-source field
mapping both writers share (issue athenaeum#2159).

:func:`yaml_scalar_literal` and :func:`set_frontmatter_scalar` were
``repair._yaml_scalar_literal`` / ``repair._set_frontmatter_scalar`` until
issue athenaeum#2159 needed the same two primitives on the INTAKE side too.
``repair`` is L4 and ``intake`` is L2, so a shared copy has to live at or
below L2; it lands here, at L1, as a leaf. ``repair`` keeps its own private
names as aliases of these, so every existing ``repair`` call site — and
``tests/test_mcp_server.py``'s ``repair._DEFAULTED_INFERRED_SOURCE``
import — is untouched.

**Why textual surgery and not a parse/render round trip.** Only the single
``^key:`` line is rewritten; every other byte of the block (key order,
quoting style, comments, blank lines) survives verbatim. A
:func:`athenaeum.models.parse_frontmatter` ->
:func:`athenaeum.models.render_frontmatter` round trip would reflow keys the
edit never meant to touch, so a second run would differ from the first on
bytes nobody asked it to change. That is the ``repair --legacy-source-slugs``
discipline, and the same reasoning
:func:`athenaeum.intake.persist_recovered_origin_session_id` records.

**One owner for the witnessed rewrite.** :func:`witnessed_source_fields` is
the ONLY place the ``witnessed`` provenance fields are composed, and the only
place the ``#turn<N>`` ref is built. Both writers call it — intake's
:func:`athenaeum.intake.persist_witnessed_source` and
``repair --backfill-sources`` (:func:`athenaeum.repair.backfill_sources`) —
which is what makes "the two rewrites are identical" (issue athenaeum#2159
AC 5) structural rather than two transcriptions free to drift. The ref comes
from :func:`athenaeum.transcript_verify._best_effort_ref`, CALLED rather than
re-derived: the MEMORY's ``originTurn`` and the witness ledger's own
per-session ``turn`` counter are DIFFERENT numberings, and only the former
belongs in a ``#turn`` ref (AC 6). The ledger's counter surfaces only inside
``witness_ref``, as its ``seq`` segment.

Layering: L1 primitive. Imports only :mod:`athenaeum.transcript_verify` (also
L1) for that one ref builder, plus stdlib — no config, no LLM client, no I/O.
Every function here is PURE: reading the file and writing it back belongs to
the caller (see ``intake``'s ``atomic_write_text`` call site), which is what
keeps this module usable from both an L2 and an L4 writer.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from athenaeum.transcript_verify import _best_effort_ref

__all__ = [
    "DEFAULTED_INFERRED_SOURCE",
    "WITNESSED_SOURCE_TYPE",
    "apply_frontmatter_scalars",
    "set_frontmatter_scalar",
    "witnessed_source_fields",
    "yaml_scalar_literal",
]

#: The wiki-level ``source`` scalar :func:`athenaeum.mcp_server.remember`
#: stamps when a caller supplies no ``sources``. KEEP BYTE-IDENTICAL to
#: ``mcp_server._DEFAULT_INFERRED_SOURCE``: both ``repair --backfill-sources``
#: (issue athenaeum#328, via its ``repair._DEFAULTED_INFERRED_SOURCE`` alias)
#: and the witnessed rewrite (issue athenaeum#2159) select candidates by
#: matching it EXACTLY — a caller-declared source is never second-guessed.
DEFAULTED_INFERRED_SOURCE = "claude:inferred"

#: The ``source_type`` channel a witnessed rewrite stamps. A member of
#: :data:`athenaeum.models.SOURCE_TYPES` (issue athenaeum#2158 added it);
#: the literal lives here rather than importing ``models`` so this module
#: stays a leaf, and ``tests/test_frontmatter_edit.py`` asserts the two agree.
WITNESSED_SOURCE_TYPE = "witnessed"


def yaml_scalar_literal(value: str) -> str:
    """Render *value* as a YAML scalar RHS, single-quoting when unsafe as plain.

    Source scalars (``user:...``, ``witnessed:...``, ``agent-observed:...``)
    and refs (``<session>#turn<N>``) carry ``:`` / ``#`` that YAML would
    otherwise misparse, so they are single-quoted (with ``'`` doubled). Bare
    model ids and channel labels stay plain.
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


def set_frontmatter_scalar(fm: str, key: str, value_literal: str) -> str:
    """Replace the top-level ``key:`` line's value, or append the key.

    Only the single ``key:`` line is rewritten; every other line is preserved
    byte-for-byte (the ``repair --legacy-source-slugs`` discipline). *fm* is a
    frontmatter body WITHOUT the ``---`` fences and without a trailing newline
    (as returned by ``repair._split_frontmatter``, or as captured by
    ``intake._ORIGIN_SESSION_FRONTMATTER_RE``'s group 1). The anchored
    ``^key:`` match never touches an indented (nested) key of the same name.

    Both callers reach this through ``Path.read_text``, which reads in
    universal-newline mode, so *fm* is always LF-separated by the time it
    arrives — there is deliberately no CRLF handling here to go stale.
    """
    pattern = re.compile(rf"^{re.escape(key)}:.*$", re.MULTILINE)
    replacement = f"{key}: {value_literal}"
    new_fm, n = pattern.subn(lambda _m: replacement, fm, count=1)
    if n:
        return new_fm
    sep = "\n" if fm else ""
    return fm + sep + replacement


def apply_frontmatter_scalars(fm: str, fields: Mapping[str, str]) -> str:
    """:func:`set_frontmatter_scalar` over *fields*, in iteration order.

    Each value is passed through :func:`yaml_scalar_literal` — which is a
    no-op for the plain-safe ones, so this is byte-identical to quoting each
    value by hand at the call site, and removes the chance of one writer
    quoting a field the other left bare.
    """
    for key, value in fields.items():
        fm = set_frontmatter_scalar(fm, key, yaml_scalar_literal(value))
    return fm


def witnessed_source_fields(
    *, session_id: str, origin_turn: int | None, witness_ref: str | None
) -> dict[str, str]:
    """The frontmatter fields a ``witnessed`` attestation persists.

    THE one composer of this mapping (issue athenaeum#2159 AC 5): intake and
    ``repair --backfill-sources`` both call it, so the two rewrites cannot
    differ. Returns, in write order:

    * ``source`` — ``witnessed:<session>#turn<N>``, the ranked tier-1 token
      the resolver's source-precedence taxonomy keys on (the SCALAR is what
      ranks, not ``source_type``).
    * ``source_type`` — :data:`WITNESSED_SOURCE_TYPE`.
    * ``source_ref`` — the bare ``<session>#turn<N>``.
    * ``witness_ref`` — ``<ledger_id>:<seq>``, omitted entirely when
      *witness_ref* is ``None``/empty (never written as an empty value).

    ``<N>`` is *origin_turn* — the MEMORY's ``originTurn`` — resolved through
    :func:`athenaeum.transcript_verify._best_effort_ref`, the same function
    ``repair``'s ``user:<ref>`` path already goes through, so the two refs are
    the same numbering by construction (AC 6). When *origin_turn* is ``None``
    the ref is the bare ``<session>``. The witness ledger's own per-session
    ``turn`` counter is a DIFFERENT numbering and must never reach here; it
    appears only as the ``seq`` segment of *witness_ref*.

    Raises :class:`ValueError` on an empty *session_id*: there would be no ref
    to cite, and ``source: witnessed:`` is not a legal source scalar
    (:func:`athenaeum.provenance.parse_source` rejects an empty ref). Callers
    treat "no origin session" as a skip, not as a rewrite with a blank ref.
    """
    ref = _best_effort_ref(session_id, origin_turn)
    if not ref:
        raise ValueError(
            "witnessed_source_fields requires a non-empty session_id — a "
            "witnessed rewrite with no session ref would write the illegal "
            "source scalar 'witnessed:' (issue athenaeum#2159)"
        )
    fields = {
        "source": f"{WITNESSED_SOURCE_TYPE}:{ref}",
        "source_type": WITNESSED_SOURCE_TYPE,
        "source_ref": ref,
    }
    if witness_ref:
        fields["witness_ref"] = witness_ref
    return fields
