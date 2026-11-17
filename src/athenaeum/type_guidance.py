# SPDX-License-Identifier: Apache-2.0
"""Per-type Tier-3 librarian guidance and template overrides — L4 domain/pipeline.

Contract: loads, validates, fingerprints and renders the operator-authored
``wiki/_schema/_type-guidance-<type>.md`` and ``_entity-template-<type>.md``
fragments (issue athenaeum#2590) that the Tier-3 create and merge builders
inject. Split out of :mod:`athenaeum.tiers` (issue athenaeum#2731) as a pure
move; every name here is still importable from ``athenaeum.tiers``.

The fragment reader :func:`athenaeum.tiers._load_schema_text` (size cap and
``<user_document>`` defanging) is shared with the global schema fragments, so
it stays in ``tiers`` and is reached through :func:`_tiers`, a deferred
import that breaks the otherwise circular module dependency and keeps a
``monkeypatch`` of ``tiers._load_schema_text`` effective here.
"""

from __future__ import annotations

import hashlib
import importlib.resources
import logging
import re
from pathlib import Path
from types import ModuleType
from typing import NamedTuple

from athenaeum.models import load_schema_list

log = logging.getLogger(__name__)


def _tiers() -> ModuleType:
    """Return :mod:`athenaeum.tiers` (deferred: it imports this module)."""
    from athenaeum import tiers

    return tiers

#: Filename prefix of an operator-authored per-type guidance fragment,
#: ``wiki/_schema/_type-guidance-<type>.md``. Flat and underscore-prefixed on
#: purpose: the page walkers that use ``rglob`` with a filename-only ``_``
#: check would sweep a ``types/`` subdirectory of non-underscore names.
TYPE_GUIDANCE_PREFIX = "_type-guidance-"

#: Filename prefix of an operator-authored per-type create template,
#: ``wiki/_schema/_entity-template-<type>.md``. Replaces the global
#: ``_entity-template.md`` for creates of that type.
TYPE_TEMPLATE_PREFIX = "_entity-template-"

#: A type is only ever used to build a path after it passes this shape check
#: AND appears in the declared ``types.md`` list (issue athenaeum#2590).
_TYPE_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


class TypeGuidance(NamedTuple):
    """The per-type fragments the Tier-3 builders inject (issue athenaeum#2590).

    Both fields are ``""`` when the type has no file, is invalid, or is not
    declared — the builders treat ``""`` as "render nothing", which is what
    keeps a wiki without these files byte-identical to before.
    """

    guidance: str
    template: str


_NO_TYPE_GUIDANCE = TypeGuidance("", "")


def _has_any_type_fragment(schema_dir: Path) -> bool:
    """True when ``_schema/`` holds at least one per-type fragment file."""
    try:
        for child in schema_dir.iterdir():
            name = child.name
            if name.endswith(".md") and (
                name.startswith(TYPE_GUIDANCE_PREFIX) or name.startswith(TYPE_TEMPLATE_PREFIX)
            ):
                return True
    except OSError:
        return False
    return False


def resolve_type_guidance(wiki_root: Path | None, entity_type: str | None) -> TypeGuidance:
    """Return the operator's guidance text and template override for *entity_type*.

    Issue athenaeum#2590. Reads ``wiki/_schema/_type-guidance-<type>.md`` and
    ``wiki/_schema/_entity-template-<type>.md`` through
    :func:`_load_schema_text`, so both are capped at
    :data:`_SCHEMA_FRAGMENT_MAX_CHARS` and defanged exactly like the global
    fragments.

    The type comes from LLM output over untrusted text (create) or from page
    frontmatter (merge), so no path is built from it until it matches
    :data:`_TYPE_KEY_RE` and is listed in the declared ``types.md``. Any other
    type (``../x``, ``Recipe``, an undeclared class) yields empty fragments and
    a warning. When ``_schema/`` holds no per-type files at all, this returns
    empty without reading ``types.md`` or logging anything, so a wiki that
    never opted in pays nothing.
    """
    if wiki_root is None or not entity_type:
        return _NO_TYPE_GUIDANCE
    schema_dir = Path(wiki_root) / "_schema"
    if not _has_any_type_fragment(schema_dir):
        return _NO_TYPE_GUIDANCE
    if not _TYPE_KEY_RE.match(entity_type):
        log.warning(
            "type-guidance: ignoring type %r: not a valid type key (issue athenaeum#2590)",
            entity_type,
        )
        return _NO_TYPE_GUIDANCE
    if entity_type not in load_schema_list(schema_dir, "types.md"):
        log.warning(
            "type-guidance: ignoring type %r: not declared in types.md (issue athenaeum#2590)",
            entity_type,
        )
        return _NO_TYPE_GUIDANCE
    return TypeGuidance(
        guidance=_tiers()._load_schema_text(wiki_root, f"{TYPE_GUIDANCE_PREFIX}{entity_type}.md"),
        template=_tiers()._load_schema_text(wiki_root, f"{TYPE_TEMPLATE_PREFIX}{entity_type}.md"),
    )


def type_guidance_state(wiki_root: Path) -> dict[str, str]:
    """Report ``{type: sha256_hex}`` for each declared type that has a per-type file.

    Issue athenaeum#2590: the input to the run summary's ``type_guidance=``
    token. The hash covers the guidance file and the template override
    together, so an edit to either changes it. Only types the builders would
    actually use (valid key, declared in ``types.md``) are reported, in
    sorted order. Empty when ``_schema/`` holds no per-type files.
    """
    schema_dir = Path(wiki_root) / "_schema"
    if not _has_any_type_fragment(schema_dir):
        return {}
    state: dict[str, str] = {}
    for entity_type in sorted(set(load_schema_list(schema_dir, "types.md"))):
        if not _TYPE_KEY_RE.match(entity_type):
            continue
        parts: list[bytes] = []
        found = False
        for prefix in (TYPE_GUIDANCE_PREFIX, TYPE_TEMPLATE_PREFIX):
            path = schema_dir / f"{prefix}{entity_type}.md"
            try:
                data = path.read_bytes()
                found = True
            except OSError:
                data = b""
            parts.append(prefix.encode() + b"\0" + data)
        if found:
            state[entity_type] = hashlib.sha256(b"\0\0".join(parts)).hexdigest()
    return state


def _load_type_guidance_prompt() -> str:
    """Read the type-guidance precedence clause (issue athenaeum#2590).

    Prompt text is content, not code: the clause lives in
    ``src/athenaeum/prompts/type_guidance.md`` and is bound to
    :data:`TYPE_GUIDANCE_PROMPT` at import time, the same convention as
    :func:`_load_relevance_category_prompt`.
    """
    resource = importlib.resources.files("athenaeum.prompts").joinpath("type_guidance.md")
    return resource.read_text(encoding="utf-8")


#: The precedence clause rendered ahead of an operator's per-type guidance,
#: and only when a guidance file exists. Registered in
#: ``athenaeum.prompt_registry`` as ``type_guidance.type_guidance_prompt``.
TYPE_GUIDANCE_PROMPT = _load_type_guidance_prompt()


def render_type_guidance_section(entity_type: str | None, guidance: str) -> str:
    """Render the Tier-3 ``{type_guidance_section}`` slot (issue athenaeum#2590).

    ``""`` when there is no guidance, which leaves every Tier-3 prompt
    byte-identical to before this issue. The one-line heading stays inline
    (it only names the type); the clause text lives in the package prompt.
    """
    if not guidance or not entity_type:
        return ""
    return f"\n## Guidance for `{entity_type}` pages\n{TYPE_GUIDANCE_PROMPT}{guidance}\n"
