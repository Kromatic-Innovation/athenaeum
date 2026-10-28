# SPDX-License-Identifier: Apache-2.0
"""promptwitness — a harness-agnostic prompt witness ledger (athenaeum#2147).

This package is written to be extractable later as a standalone OSS library
and CLI (athenaeum#2162): it imports nothing from ``athenaeum`` (enforced by
``tests/test_promptwitness_boundary.py``'s AST scan), is standard-library
only at import time, and contains no operator paths, no 1Password item
names, no ``~/knowledge`` references and no Kromatic identifiers.

Dependency direction is one way only: ``athenaeum`` may depend on
``promptwitness``; this package never depends back.

Submodules (see ``docs/design/witness-ledger.md`` for the full design):

- :mod:`promptwitness.canon` — text normalization, digests, canonical JSON.
- :mod:`promptwitness.ledger` — the hash-chained witness ledger (v1 schema),
  appender, tolerant reader, chain verifier, and the ``LedgerIndex`` lookup.
- :mod:`promptwitness.cli` — the ``promptwitness`` console script.

This module intentionally imports none of its own submodules at package
import time, so a bare ``import promptwitness`` costs nothing beyond this
docstring and ``__version__`` — the same "lazy root" discipline
``athenaeum``'s own package root follows.
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__"]
