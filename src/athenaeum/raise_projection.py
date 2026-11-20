# SPDX-License-Identifier: Apache-2.0
"""The pure raise projection, payload hash, id minting and derived uid (athenaeum#2798).

A raise copies one page from an origin store to a target store. This module
holds the four primitives every later slice of the raise and retract protocol
builds on (``docs/design/federation-raise-retract.md`` §4.2, §4.4, §6.1, §14
slice S1):

* :func:`project` / :func:`render` -- reduce a page to what a raise carries;
* :func:`payload_hash` -- a hash of the projection, never of a whole page;
* :func:`mint_raise_id` / :func:`mint_message_id` and their validators;
* :func:`derived_uid` -- the uid a target store gives the copy of a raise.

The payload hash is defined over the projection on BOTH sides: the raiser
hashes the projection of the source page, and the target hashes the projection
of the copy as it stands now. The projection drops ``uid``, ``sources``,
``created``, ``updated`` and ``raised_from``, so an unedited copy compares
equal to the hash that travelled. ``content_hash`` over a whole copy would not.

Pure: no file access, no git, no model call, no store or config import.

Layering: L2. Imports :mod:`athenaeum.models` (L1) and :mod:`athenaeum.verdicts`
(L2); nothing above.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from dataclasses import dataclass, field
from typing import Any

from athenaeum.models import parse_frontmatter, render_frontmatter
from athenaeum.verdicts import content_hash

#: The frontmatter keys a raise carries, in the fixed order they are rendered.
PROJECTED_KEYS: tuple[str, ...] = (
    "type",
    "name",
    "description",
    "aliases",
    "tags",
    "access",
    "audience",
)

#: Domain-separation tag of the derived uid (§4.4).
DERIVED_UID_DOMAIN = "athenaeum-raise-v1"

RAISE_ID_PREFIX = "rs_"
MESSAGE_ID_PREFIX = "rm_"
_ID_ALPHABET = "abcdefghijklmnopqrstuvwxyz234567"
_ID_LEN = 20  # 20 base32 characters = 100 random bits
_RAISE_ID_RE = re.compile(r"^rs_[a-z2-7]{20}\Z")
_MESSAGE_ID_RE = re.compile(r"^rm_[a-z2-7]{20}\Z")

# The corpus' wikilink and markdown-inline-link grammar, as
# :mod:`athenaeum.store_links` states it. Restated rather than imported:
# that module imports config and storage, which this module must not.
_WIKILINK_RE = re.compile(r"\[\[([^\[\]|\n]+?)(?:\|([^\[\]\n]*))?\]\]")
_MD_LINK_RE = re.compile(r"\[([^\[\]\n]*)\]\(\s*<?([^)<>\s]+)>?\s*\)")

# A page reference stem: a uid (16 hex characters, or the 8 hex characters
# older pages carry), optionally followed by ``-<slug>``.
_UID_STEM_RE = re.compile(r"^(?:[0-9a-f]{16}|[0-9a-f]{8})(?:-.+)?\Z")
_URL_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")


@dataclass(frozen=True)
class Projection:
    """What a raise carries: the kept keys (fixed order) and the linked-free body."""

    meta: dict[str, Any] = field(default_factory=dict)
    body: str = ""


def _names_a_page_by_uid(target: str) -> bool:
    """True when *target* (anchor/query ignored) is a uid or ``<uid>-<slug>`` page.

    Any directory prefix and a trailing ``.md`` are ignored.
    """
    path = target.split("#", 1)[0].split("?", 1)[0].strip()
    base = path.rsplit("/", 1)[-1]
    if base.endswith(".md"):
        base = base[: -len(".md")]
    return _UID_STEM_RE.match(base) is not None


def _wikilink_sub(m: re.Match[str]) -> str:
    target = m.group(1).strip()
    label = m.group(2)
    if label is not None and label.strip():
        return label.strip()
    if _names_a_page_by_uid(target):
        return ""
    return target


def _md_link_sub(m: re.Match[str]) -> str:
    target = m.group(2)
    if _URL_SCHEME_RE.match(target):
        return m.group(0)
    page = target.split("#", 1)[0].split("?", 1)[0]
    if page.endswith(".md") or _names_a_page_by_uid(page):
        return m.group(1)
    return m.group(0)


def _strip_links(body: str) -> str:
    return _MD_LINK_RE.sub(_md_link_sub, _WIKILINK_RE.sub(_wikilink_sub, body))


def has_link(body: str) -> bool:
    """True when *body* carries a link the projection would strip (§4.2).

    A copy accepted whole must keep its body unchanged, so acceptance refuses
    a payload that still has a page link rather than stripping it.
    """
    return _strip_links(body) != body


def project(page_text: str) -> Projection:
    """Reduce *page_text* to its :class:`Projection` (§4.2). Deterministic."""
    meta, body = parse_frontmatter(page_text)
    if not isinstance(meta, dict):
        meta = {}
    kept = {k: meta[k] for k in PROJECTED_KEYS if k in meta}
    return Projection(meta=kept, body=_strip_links(body))


def render(projection: Projection) -> str:
    """Render *projection* as a page: kept keys in fixed order, then the body."""
    meta = {k: projection.meta[k] for k in PROJECTED_KEYS if k in projection.meta}
    return render_frontmatter(meta) + projection.body


def payload_hash(page_text: str) -> str:
    """``"sha256:"`` plus :func:`content_hash` of the rendered projection (§4.2)."""
    return "sha256:" + content_hash(render(project(page_text)))


def _mint(prefix: str) -> str:
    return prefix + "".join(secrets.choice(_ID_ALPHABET) for _ in range(_ID_LEN))


def mint_raise_id() -> str:
    """A fresh ``raise_id``: ``rs_`` plus 20 random lowercase base32 characters."""
    return _mint(RAISE_ID_PREFIX)


def mint_message_id() -> str:
    """A fresh ``message_id``: ``rm_`` plus 20 random lowercase base32 characters."""
    return _mint(MESSAGE_ID_PREFIX)


def is_raise_id(value: object) -> bool:
    """True when *value* has the shape of a ``raise_id``."""
    return isinstance(value, str) and _RAISE_ID_RE.match(value) is not None


def is_message_id(value: object) -> bool:
    """True when *value* has the shape of a ``message_id``."""
    return isinstance(value, str) and _MESSAGE_ID_RE.match(value) is not None


def derived_uid(target_store_id: str, raise_id: str) -> str:
    """The uid the target store gives the copy of *raise_id* (§4.4).

    The first 16 hex characters of
    ``sha256("athenaeum-raise-v1" + NUL + target_store_id + NUL + raise_id)``.
    Not random, so it does not call ``generate_uid``. The shape of
    *target_store_id* is validated elsewhere (athenaeum#2772), not here.
    """
    material = "\x00".join((DERIVED_UID_DOMAIN, target_store_id, raise_id))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]
