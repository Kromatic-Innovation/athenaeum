# SPDX-License-Identifier: Apache-2.0
"""Ephemeral / operational auto-memory classifier (issue #278).

A content-aware gate that keeps inherently-throwaway operational session
notes (staging / worktree / deploy / CI / install-token boilerplate, plus
throwaway temp-dir scopes) out of the durable wiki. Before #278 the
auto-memory compile pipeline was content-agnostic: every file under
``raw/auto-memory/<scope>/`` matching the auto-memory naming convention was
clustered and materialized into a permanent ``type: auto-memory``
``wiki/auto-*.md`` page. This module supplies the missing classifier.

Two entry points, sharing one precision order:

* :func:`classify_ephemeral` -- for a RAW intake file (one scope string +
  its frontmatter + body). Used at the discover choke point and as a
  secondary guard at merge.
* :func:`classify_ephemeral_page` -- for a COMPILED ``wiki/auto-*.md`` page
  (its ``origin_scopes`` LIST + body). Used by the prune driver.

Precision order (most authoritative first):

1. an explicit ``ephemeral: true`` frontmatter flag -- the long-term-correct,
   highest-precision signal (the producer self-declares the memory throwaway);
2. the scope matches a configured ephemeral-scope glob;
3. a MULTI-SIGNAL operational-marker match (>= 2 distinct markers) -- only
   when the operator opts in via ``librarian.operational_markers`` (default
   empty), so a legit architecture note is never clobbered by one word.

Every classifier returns a human-readable REASON string when the intake is
ephemeral, else ``None`` -- so callers can log *why* something was dropped.
"""

from __future__ import annotations

from fnmatch import fnmatch
from typing import Any

# Frontmatter-flag truthy spellings honored for ``ephemeral:``.
_TRUTHY: frozenset[str] = frozenset({"true", "1", "yes", "on"})

# Minimum distinct operational markers that must co-occur before the
# marker signal alone classifies an intake as ephemeral (issue #278).
# Kept conservative: a single incidental word must never drop a real note.
_MIN_MARKER_SIGNALS = 2


def _flag_is_ephemeral(meta: dict[str, Any] | None) -> bool:
    """True when frontmatter declares an explicit truthy ``ephemeral`` flag."""
    if not isinstance(meta, dict):
        return False
    flag = meta.get("ephemeral")
    if flag is True:
        return True
    if isinstance(flag, str) and flag.strip().lower() in _TRUTHY:
        return True
    return False


def _scope_matches(scope: str, ephemeral_scopes: list[str]) -> str | None:
    """Return the first glob *scope* matches, else ``None``."""
    if not scope:
        return None
    for glob in ephemeral_scopes:
        if glob and fnmatch(scope, glob):
            return glob
    return None


def _marker_hits(
    meta: dict[str, Any] | None,
    body: str,
    operational_markers: list[str],
) -> list[str]:
    """Distinct operational markers present in name/description/body (lower-cased)."""
    markers = [m for m in operational_markers if m]
    if not markers:
        return []
    parts: list[str] = []
    if isinstance(meta, dict):
        parts.append(str(meta.get("name", "")))
        parts.append(str(meta.get("description", "")))
    parts.append(body or "")
    hay = " ".join(parts).lower()
    return sorted({m for m in markers if m in hay})


def classify_ephemeral(
    scope: str,
    meta: dict[str, Any] | None,
    body: str,
    *,
    ephemeral_scopes: list[str],
    operational_markers: list[str],
) -> str | None:
    """Classify a single RAW auto-memory intake file (issue #278).

    Args:
        scope: the auto-memory scope DIRECTORY NAME (e.g.
            ``-Users-alice-Code-projectx`` or a throwaway temp-dir hash).
        meta: parsed frontmatter dict (or ``None``).
        body: the file body (post-frontmatter); used only for markers.
        ephemeral_scopes: glob patterns from
            :func:`athenaeum.config.resolve_ephemeral_scopes`.
        operational_markers: substrings from
            :func:`athenaeum.config.resolve_operational_markers`.

    Returns:
        A drop-reason string when the intake is ephemeral, else ``None``.
    """
    if _flag_is_ephemeral(meta):
        return "explicit ephemeral:true frontmatter flag"
    glob = _scope_matches(scope, ephemeral_scopes)
    if glob is not None:
        return f"ephemeral scope (matched glob {glob!r})"
    hits = _marker_hits(meta, body, operational_markers)
    if len(hits) >= _MIN_MARKER_SIGNALS:
        return f"operational markers matched ({', '.join(hits)})"
    return None


def classify_ephemeral_page(
    meta: dict[str, Any] | None,
    body: str,
    *,
    ephemeral_scopes: list[str],
    operational_markers: list[str],
) -> str | None:
    """Classify a COMPILED ``wiki/auto-*.md`` page (issue #278, prune driver).

    Same precision order as :func:`classify_ephemeral`, but the scope signal
    reads the page's ``origin_scopes`` LIST: the page is classified ephemeral
    only when it carries at least one origin scope AND **every** origin scope
    is ephemeral. A page mixing a throwaway scope with a real one is RETAINED
    -- the conservative call, so the kill-list never removes a page that also
    captured legitimate knowledge.

    Returns a kill-reason string when the page is operational/ephemeral, else
    ``None``.
    """
    if _flag_is_ephemeral(meta):
        return "explicit ephemeral:true frontmatter flag"

    scopes_raw = meta.get("origin_scopes") if isinstance(meta, dict) else None
    scope_list = (
        [str(s) for s in scopes_raw if str(s).strip()]
        if isinstance(scopes_raw, list)
        else []
    )
    if scope_list and ephemeral_scopes:
        matched: list[str] = []
        for s in scope_list:
            glob = _scope_matches(s, ephemeral_scopes)
            if glob is None:
                matched = []
                break
            matched.append(s)
        if matched:
            return f"all origin scopes ephemeral ({', '.join(matched)})"

    hits = _marker_hits(meta, body, operational_markers)
    if len(hits) >= _MIN_MARKER_SIGNALS:
        return f"operational markers matched ({', '.join(hits)})"
    return None
