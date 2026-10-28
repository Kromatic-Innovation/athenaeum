# SPDX-License-Identifier: Apache-2.0
"""Harness-specific promptwitness adapters (athenaeum#2149).

Each submodule turns one harness's own hook stdin/env shape into a
:func:`promptwitness.ledger.append_line` call -- the harness-agnostic
``promptwitness.ledger``/``promptwitness.canon`` modules know nothing about
any of them. Zero ``athenaeum`` imports anywhere under this package, same as
the rest of ``promptwitness`` (enforced by
``tests/test_promptwitness_boundary.py``).

- :mod:`promptwitness.harness.claude_code` -- Claude Code's
  ``UserPromptSubmit``/``SessionEnd`` hooks.

This package root imports none of its own submodules at import time, for the
same reason :mod:`promptwitness` itself does not.
"""

from __future__ import annotations

__all__: list[str] = []
