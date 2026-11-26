# SPDX-License-Identifier: Apache-2.0
"""One predicate for whether a principal holds a right on a location.

Layering: L3 (imports :mod:`athenaeum.store_rights`, :mod:`athenaeum.store_access`
and :mod:`athenaeum.config`, L2, and :mod:`athenaeum.storage` and
:mod:`athenaeum.models`, L1).

Design: ``docs/design/federation-store-access.md`` §16.

:func:`can` answers from the location's store declaration with the existing
parser and rights resolution. It adds no right and reads no policy file. It
returns a boolean, never a role name, and nothing here keys on a role.

* A valid declaration: the union of the rights of every role that lists the
  principal directly or through a group.
* No declaration: the install's owner (the identity key of ``owner.asserter``)
  holds every right; everyone else holds none.
* An invalid declaration, or a directory that cannot answer: nobody but the
  owner holds read, and the owner holds read only. Failure narrows.

Resolution is held per process by :mod:`athenaeum.store_rights`. Do not call
:func:`can` from the recall query path.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from athenaeum.config import resolve_owner_asserter
from athenaeum.models import asserter_identity_key
from athenaeum.storage import available_adapters
from athenaeum.store_access import RIGHTS
from athenaeum.store_rights import READER_ONLY, held_identity_rights

__all__ = ["can"]


def can(
    principal: Sequence[str] | None,
    location: str,
    right: str,
    *,
    config: dict[str, Any] | None,
    knowledge_root: Path,
) -> bool:
    """Whether the identity key *principal* holds *right* on the named *location*.

    Raises ``ValueError`` for a right outside ``read, propose, compile, answer``
    and for a location the configuration does not name. An empty principal is
    ``False``.
    """
    if right not in RIGHTS:
        raise ValueError(f"unknown right {right!r}: expected one of {', '.join(RIGHTS)}")
    adapters = available_adapters(config)
    if location not in adapters:
        raise ValueError(f"unknown location {location!r}")
    if not principal or isinstance(principal, str):
        return False
    identity = tuple(principal)
    root = adapters[location].resolve_root(knowledge_root)
    owner = tuple(asserter_identity_key(resolve_owner_asserter(config)))
    is_owner = bool(owner) and identity == owner

    rights, narrowed = held_identity_rights(root, identity)
    if rights is None:
        return is_owner
    if narrowed:
        return is_owner and right in READER_ONLY
    return right in rights
