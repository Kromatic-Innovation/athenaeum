# SPDX-License-Identifier: Apache-2.0
"""Subpackage holding scaffold `*.md` templates (company/concept/person/project/source).

Contract: pure data — human-editable scaffold frontmatter for `athenaeum init
--with-templates` to copy verbatim into a fresh knowledge dir. Not an
LLM-facing schema (see `athenaeum.schema` for that); each template's own
`# Scaffold template ...` comment line makes this explicit to a human editor.

Factoring rule: only template `*.md` files and this docstring belong here.
No Python logic — the copy mechanics live in `athenaeum.init`, which reads
these files as package data. Sits below L1 (no imports of its own; nothing
in athenaeum imports FROM here except path/resource lookups in
`athenaeum.init`).
"""
