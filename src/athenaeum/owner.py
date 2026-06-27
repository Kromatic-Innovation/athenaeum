# SPDX-License-Identifier: Apache-2.0
"""Owner-aware intake routing (issue #263, slice D of #259).

Centralises the rule for where an owner-namespace memory lands when the
librarian compiles raw intake into the wiki. Person-bio facts fold into the
canonical owner person page; operational/exclusion memories (e.g. a family
relationships list — owner-confirmed 2026-06-26) are NOT person-bio and must
route to a standalone ``reference`` page instead of polluting the owner bio.

Owner identity is supplied entirely by config (see
:func:`athenaeum.config.resolve_owner`); when no owner is configured every
function here is inert (returns ``None``) so the shipped package carries no
personal identity.
"""

from __future__ import annotations

from typing import Any

# Substrings in an owner-namespace memory name that mark it as an
# operational / exclusion list (relationship inventories, contact / block
# lists) rather than person-bio. These route to a dedicated ``reference``
# page. Conservative on purpose — anything not matched stays person-bio.
_OWNER_REFERENCE_MARKERS: tuple[str, ...] = (
    "family",
    "relationship",
    "exclusion",
    "exclude",
    "blocklist",
    "do_not",
    "do-not",
    "operational",
)


def route_owner_memory(name: str, owner: dict[str, Any] | None) -> str | None:
    """Decide the wiki entity type for an owner-namespace memory.

    Returns ``"reference"`` for owner operational/exclusion memories,
    ``"person"`` for owner person-bio memories, and ``None`` when the memory
    is not in the owner namespace OR no owner is configured (inert).

    A memory is in the owner namespace when its name starts with the
    ``user_`` prefix or with one of the configured owner aliases.
    """
    if not owner or not name:
        return None
    n = name.strip().lower()
    if not n:
        return None

    in_namespace = n.startswith("user_")
    if not in_namespace:
        for alias in owner.get("aliases", []):
            al = str(alias).strip().lower()
            if al and (n == al or n.startswith(al + "_")):
                in_namespace = True
                break
    if not in_namespace:
        return None

    for marker in _OWNER_REFERENCE_MARKERS:
        if marker in n:
            return "reference"
    return "person"


__all__ = ["route_owner_memory"]
