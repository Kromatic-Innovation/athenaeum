# SPDX-License-Identifier: Apache-2.0
"""External-data connectors that enrich wiki entities.

Each connector is a thin client around a third-party API plus an
``enrich_<type>`` function that returns the fields a wiki should be
updated with — never writing the wiki itself. Composition (merge into
frontmatter, attach ``field_sources``, persist) is the caller's job so
the connectors stay testable and composable.
"""
