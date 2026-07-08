# SPDX-License-Identifier: Apache-2.0
"""Live-API eval suite (issue #331).

Every module here carries ``pytestmark = pytest.mark.eval`` — the ``eval``
marker is deselected by default (``pyproject.toml`` ``addopts``) so nothing
in the regular test suite hits the network. Runs via the ``evals.yml``
workflow (``workflow_dispatch`` + ``push: branches: [main]``) or locally
with ``pytest -m eval tests/evals/``.

Golden-set content policy: synthetic small-org scenarios only — never
maintainer-live-knowledge content. See ``README.md``.
"""
