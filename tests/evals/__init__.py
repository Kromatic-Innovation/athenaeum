# SPDX-License-Identifier: Apache-2.0
"""Live-API eval suite (issue athenaeum#331).

Most modules here carry ``pytestmark = pytest.mark.eval`` — the ``eval``
marker is deselected by default (``pyproject.toml`` ``addopts``) so nothing
in the regular test suite hits the network. Runs via the ``evals.yml``
workflow (``workflow_dispatch`` + ``push: branches: [main]``) or locally
with ``pytest -m eval tests/evals/``. A growing subset of modules under this
package are token-free (recorded-fixture renders, offline unit tests, the
containment/rollout machinery's own plumbing tests) and carry no marker at
all, or carry ``pytest.mark.rollout`` only when they genuinely spend tokens
or spawn the ``claude`` binary (issue athenaeum#1742) — see ``README.md``.

Golden-set content policy: synthetic small-org scenarios only — never
maintainer-live-knowledge content. See ``README.md``.
"""
