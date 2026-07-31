"""Subpackage holding the wiki's `*.md` schema/reference docs (types, tags,
access-levels, observation-filter, entity template).

Contract: pure reference data read by humans and by the librarian's Tier 3
writer — controlled vocabularies (`types.md`, `tags.md`), the access-level
taxonomy (`access-levels.md`), the meta-memory observation policy
(`observation-filter.md`), and the canonical new-entity-page shape
(`_entity-template.md`).

Factoring rule: only reference `*.md` docs and this docstring belong here.
No Python logic and no scaffold templates (those are `athenaeum.templates`,
a distinct "copy this to start a page" concern vs. this subpackage's
"controlled vocabulary the validator/writer consults" concern). Sits below
L1 (no imports of its own); `athenaeum.schemas` (the pydantic validators,
note the singular/plural naming distinction) and the librarian read these
files at runtime rather than importing from this package.
"""
