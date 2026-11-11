# SPDX-License-Identifier: Apache-2.0
"""Live-binary integration harnesses (issue athenaeum#2235, epic athenaeum#2144).

Everything under this package drives the REAL ``claude`` binary rather than
a synthetic stdin/env fixture, and is opt-in per module via its own
``pytestmark`` skip (never the whole-suite ``rollout``/``eval`` markers,
which `pyproject.toml`'s ``addopts`` deselects outright -- a deselected
module is invisible to CI, which is exactly what this package's modules
must NOT be: they are collected and reported skipped by default).
"""
