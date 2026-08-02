#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Fail if any ``ATHENAEUM_*`` env var read by ``src/`` is undocumented (issue #688).

~19 per-stage LLM tuning env vars were read by the code and documented nowhere,
because prose ("document new env vars") does not enforce itself. This check does:
it diffs every ``ATHENAEUM_*`` name that appears in ``src/`` against the names
documented in ``docs/configuration.md`` and fails on any that is undocumented.

Two failure classes it is built to avoid — both are the "empty side reads as a
pass" trap the issue calls out:

* **Digit-blind scan.** The var names include digits (``ATHENAEUM_REASONING_T1_
  MAX_TOKENS``). A ``[A-Z_]+`` regex truncates those to a fragment and undercounts,
  which is how the original 66/56 measurement missed four reasoning-tier vars.
  The scan here is ``ATHENAEUM_[A-Z0-9_]+``.
* **Empty either side.** If the ``src/`` scan or the docs scan comes back empty
  (a moved file, a broken glob), the diff would be trivially empty and pass
  green. This check FAILS LOUDLY when either side is empty.

It reports a denominator (``N documented of M found``) even on success, so a
green result is evidence a sweep actually ran. Deliberately-internal vars are
listed in :data:`ALLOWLIST` with a one-line reason each — never silently
excluded from the scan.

Run standalone (``python scripts/check_env_docs.py``) or via
``tests/test_env_docs.py`` (which is how CI gates it). Exit 0 = all documented,
1 = undocumented vars found, 2 = a scan side was empty (loud misconfiguration).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / "src"
CONFIG_DOC = REPO_ROOT / "docs" / "configuration.md"

#: Digit-aware — the reasoning-tier vars carry a numeric tier (`T1`/`T2`), which a
#: `[A-Z_]+` scan silently truncates (issue #688).
_ENV_RE = re.compile(r"ATHENAEUM_[A-Z0-9_]+")

#: Deliberately-internal / not-operator-facing vars that are intentionally NOT in
#: docs/configuration.md. Each MUST carry a one-line reason — never a silent
#: exclusion (issue #688). Empty today: every ATHENAEUM_* read by src/ is
#: operator-facing and documented.
ALLOWLIST: dict[str, str] = {}


def _scan(text: str) -> set[str]:
    return set(_ENV_RE.findall(text))


def scan_tree(root: Path) -> set[str]:
    """Every ``ATHENAEUM_*`` name that appears in any ``*.py`` under *root*."""
    found: set[str] = set()
    for path in root.rglob("*.py"):
        found |= _scan(path.read_text(encoding="utf-8"))
    return found


def scan_docs(path: Path) -> set[str]:
    """Every ``ATHENAEUM_*`` name documented in *path*."""
    return _scan(path.read_text(encoding="utf-8"))


def undocumented(src_vars: set[str], doc_vars: set[str]) -> set[str]:
    """Names read by src/ that are neither documented nor allowlisted."""
    return src_vars - doc_vars - set(ALLOWLIST)


def main(argv: list[str] | None = None) -> int:
    src_vars = scan_tree(SRC_DIR)
    doc_vars = scan_docs(CONFIG_DOC)

    # Fail loud if either side is empty — a comparison that passes because a grep
    # matched nothing is the exact silent-pass class this check exists to prevent.
    if not src_vars:
        print(
            f"env-docs: ERROR no ATHENAEUM_* vars found under {SRC_DIR} — the scan "
            "is broken (moved src/? bad glob?); refusing to report a false pass.",
            file=sys.stderr,
        )
        return 2
    if not doc_vars:
        print(
            f"env-docs: ERROR no ATHENAEUM_* vars found in {CONFIG_DOC} — the docs "
            "scan is broken; refusing to report a false pass.",
            file=sys.stderr,
        )
        return 2

    missing = undocumented(src_vars, doc_vars)
    documented_count = len(src_vars & doc_vars) + len(set(ALLOWLIST) & src_vars)
    denom = (
        f"{documented_count} documented of {len(src_vars)} ATHENAEUM_* vars read by "
        f"src/ ({len(ALLOWLIST)} allowlisted)"
    )

    if missing:
        print(f"env-docs: FAIL — {denom}", file=sys.stderr)
        print(
            "env-docs: the following ATHENAEUM_* vars are read by src/ but are NOT "
            "documented in docs/configuration.md (add them there with a default and "
            "what they control, or allowlist with a reason in check_env_docs.py):",
            file=sys.stderr,
        )
        for name in sorted(missing):
            print(f"  - {name}", file=sys.stderr)
        return 1

    print(f"env-docs: OK — {denom}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
