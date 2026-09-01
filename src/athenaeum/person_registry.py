# SPDX-License-Identifier: Apache-2.0
"""Consult-only person registry (issue athenaeum#1183).

17,050 of 23,545 wiki pages are ``type: person`` — overwhelmingly CRM
imports (Apollo/Streak) rather than pages any live session actually reads or
edits. Treating a CRM contact record as an ordinary wiki ENTITY is what lets
it be matched by :func:`athenaeum.tiers.tier1_programmatic_match` and folded
into a full-page LLM rewrite by :func:`athenaeum.tiers.tier3_merge` — the
correctness and PII-blast-radius problem this module fixes.

This module demotes ``type: person`` pages out of the general wiki-entity
surface into a separate, consult-only registry:

- :class:`athenaeum.models.EntityIndex` no longer carries a person page's
  name/alias keys (see ``DEMOTED_NAME_MATCH_TYPES`` in that module) — so
  :func:`~athenaeum.tiers.tier1_programmatic_match` never matches one.
- Intake can still resolve and attribute a person MENTION by consulting
  :class:`PersonRegistry` directly — see
  :func:`athenaeum.identity_resolution.resolve_person_mention` and
  :func:`athenaeum.intake.attribute_person_observation`.
- A person record accepts a structured field update through
  :func:`apply_person_field_update`, wired into the existing tier-0 no-LLM
  paths (:func:`athenaeum.intake.tier0_passthrough`,
  :func:`athenaeum.librarian.tier0_handle_upsert`) via their optional
  ``person_registry=`` parameter — never through an LLM call.
- A person record is refused by the tier-3 full-page-rewrite entry points
  (:func:`athenaeum.tiers.tier3_merge`, ``tier3_merge_full``, ``tier3_write``,
  ``tier3_create``) — see ``PersonNeverLLMRewriteError`` in that module.

**NOT the source-handle registry.** :mod:`athenaeum.registry` compiles an
UNRELATED ``registry.json`` — entity uid -> external adapter handles
(Slack/GitHub/LinkedIn usernames etc.) for the fact-mining pipeline. This
module is deliberately named ``person_registry`` (never ``registry``,
``Registry``, ``build_registry``, or ``collect_handles`` — those names are
``athenaeum.registry``'s) so the two cannot be confused. A person page's
source-handle frontmatter (``linkedin_url``, ``apollo_organization_id``, …)
keeps compiling into ``registry.json`` via ``athenaeum.registry`` exactly as
it did before this module existed; this module governs where a person page's
NAME is matched and how a field update reaches it, not the handle compile.

**Demotion, not deletion — and backward compatible with an unmigrated
corpus.** Nothing here deletes a page or a field; every person record stays
on disk and stays queryable by uid through the ordinary reader paths
(:mod:`athenaeum.pii`'s excluded-read join,
:func:`athenaeum.identity_resolution.resolve_handle_query`,
:meth:`athenaeum.models.EntityIndex.get_by_uid`) exactly as before. The
one-time physical relocation of person pages in a live corpus is issue
athenaeum#1247 (blocked by this one) — :func:`resolve_person_registry_root`
defaults to the WIKI ROOT itself, so a corpus that has not yet undergone that
relocation degrades sanely: :class:`PersonRegistry` finds every person page
exactly where it already lives today. Once athenaeum#1247 physically moves
person pages elsewhere, pointing ``person_registry.root`` at the new location
is a config change, not a code change.

Layering: L2 (data-model consult service, peer to
:mod:`athenaeum.identity_resolution`). Imports :mod:`athenaeum.models` (L1)
for ``parse_frontmatter``/``resolve_page_type``/``render_frontmatter``, and
:mod:`athenaeum.atomic_io` (L0) for the write. Deliberately does NOT import
any LLM client/provider module — :func:`apply_person_field_update` cannot
make an LLM call even by accident.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date
from pathlib import Path
from typing import Any, NamedTuple

from athenaeum.atomic_io import atomic_write_text
from athenaeum.models import parse_frontmatter, render_frontmatter, resolve_page_type
from athenaeum.schemas import validate_wiki_meta

#: The one page type this registry consults. A future consult-only class
#: (were one ever added) would extend this, but athenaeum#1183 scopes to
#: `person` only — see DEMOTED_NAME_MATCH_TYPES in athenaeum.models.
PERSON_TYPE = "person"


class PersonRegistryEntry(NamedTuple):
    """One indexed person page: identity + on-disk location."""

    uid: str
    path: Path
    name: str


class PersonRegistry:
    """Consult-only index of ``type: person`` wiki pages.

    Mirrors :class:`athenaeum.models.EntityIndex`'s name/alias/uid lookup
    shape (deliberately — callers that already know that API should feel at
    home here) but indexes ONLY ``type: person`` pages found under *root*,
    and carries no create/update-classification machinery of its own — this
    is a read/consult index plus the one write primitive
    (:func:`apply_person_field_update`), not a second entity pipeline.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self._by_name: dict[str, PersonRegistryEntry] = {}
        self._by_uid: dict[str, PersonRegistryEntry] = {}
        self._load()

    def _load(self) -> None:
        if not self.root.is_dir():
            # Matches EntityIndex's tolerance of a not-yet-created root
            # (e.g. a fresh `athenaeum init` before any person page exists).
            return
        for fpath in sorted(self.root.glob("*.md")):
            if fpath.name.startswith("_"):
                continue
            try:
                text = fpath.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            meta, _ = parse_frontmatter(text)
            if not meta:
                continue
            if resolve_page_type(meta) != PERSON_TYPE:
                continue
            uid_raw = meta.get("uid", "")
            name_raw = meta.get("name", "")
            if not uid_raw or not name_raw:
                continue
            assert isinstance(uid_raw, str)
            assert isinstance(name_raw, str)
            entry = PersonRegistryEntry(uid=uid_raw, path=fpath, name=name_raw)
            self._by_uid[uid_raw] = entry
            self._by_name[name_raw.lower()] = entry

            aliases_raw = meta.get("aliases", [])
            assert isinstance(aliases_raw, Iterable)
            for alias in aliases_raw:
                if alias:
                    assert isinstance(alias, str)
                    self._by_name[alias.lower()] = entry

    def lookup(self, name: str) -> PersonRegistryEntry | None:
        """Look up a person by name or alias (case-insensitive)."""
        return self._by_name.get(name.strip().lower())

    def get_by_uid(self, uid: str) -> PersonRegistryEntry | None:
        """Look up a person by uid."""
        return self._by_uid.get(uid)

    def register(self, entry: PersonRegistryEntry) -> None:
        """Add a newly written person record to the in-memory index.

        Mirrors :meth:`athenaeum.models.EntityIndex.register` — called after
        a tier-0 write so a later raw file in the same run's batch can match
        against the record that was just created.
        """
        self._by_uid[entry.uid] = entry
        self._by_name[entry.name.lower()] = entry

    def __len__(self) -> int:
        return len(self._by_uid)

    def items(self) -> "Iterable[tuple[str, PersonRegistryEntry]]":
        """Iterate over ``(name_or_alias_key, PersonRegistryEntry)`` pairs."""
        return self._by_name.items()


def apply_person_field_update(
    entry_path: Path,
    updates: dict[str, Any],
    *,
    dry_run: bool = False,
) -> tuple[dict[str, Any], bool]:
    """Apply a structured frontmatter field update to an existing person
    page, LLM-free (issue athenaeum#1183 AC3).

    Generalizes :func:`athenaeum.librarian.tier0_handle_upsert`'s merge+write
    mechanics (previously scoped to just
    :data:`athenaeum.registry.SOURCE_HANDLE_KEYS`) to any structured field —
    this function never inspects *which* keys are in *updates*, it only
    merges them onto the existing frontmatter and writes. This module does
    not import an LLM client/provider at all, so no call chain through this
    function can ever reach one.

    Idempotent: when every key in *updates* already matches the page's
    current value, the page is left byte-for-byte unchanged (no rewrite, no
    ``updated`` bump) and this returns ``(current_meta, False)``. Otherwise
    the merged frontmatter is schema-validated
    (:func:`athenaeum.schemas.validate_wiki_meta`), ``updated`` is stamped to
    today, and (unless *dry_run*) the page is rewritten — returning
    ``(merged_meta, True)``.
    """
    text = entry_path.read_text(encoding="utf-8")
    meta, body = parse_frontmatter(text)
    changed = any(meta.get(key) != value for key, value in updates.items())
    if not changed:
        return meta, False

    merged = dict(meta)
    merged.update(updates)
    merged["updated"] = date.today().isoformat()
    validate_wiki_meta(merged)

    if dry_run:
        return merged, True

    atomic_write_text(entry_path, render_frontmatter(merged) + "\n" + body)
    return merged, True
