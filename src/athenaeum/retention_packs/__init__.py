# SPDX-License-Identifier: Apache-2.0
"""Packaged retention-policy packs (athenaeum#985 AC9).

Data only — no Python logic lives here. Each ``*.yaml`` sibling is one
retention pack: rules keyed by ``(memory_class, data_class, jurisdiction)``
mapping to a retention action. Loaded by
:func:`athenaeum.erasure.available_retention_packs` via
``importlib.resources``, the same packaged-data-file pattern
``src/athenaeum/rule_examples/`` already uses (see that directory's
``pyproject.toml`` wheel-``include`` entry, which this directory's entry
mirrors).

Packs are data, never code (issue athenaeum#985's own AC9 constraint) — an
operator overrides or adds a pack via ``erasure.retention_packs.<name>`` in
``athenaeum.yaml``, never by patching this repository.
"""
