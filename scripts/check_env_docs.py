#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Fail if any ``ATHENAEUM_*`` env var is undocumented, or documented but unread
(issues athenaeum#688, athenaeum#1376).

~19 per-stage LLM tuning env vars were read by the code and documented nowhere,
because prose ("document new env vars") does not enforce itself. This check
runs the diff in **both directions**:

1. **src -> docs**: every ``ATHENAEUM_*`` name that appears in ``src/`` must be
   documented in ``docs/configuration.md`` (the original athenaeum#688 gate).
2. **docs -> code**: every ``ATHENAEUM_*`` name documented in
   ``docs/configuration.md`` must actually be read by something — ``src/``,
   the shipped hooks/scripts, or a known runtime-constructed family (athenaeum#1376).
   Direction 1 alone lets an operator set a documented variable that nothing
   reads and get no error and no effect.

Failure classes this is built to avoid — all are the "empty/blind side reads
as a pass" trap the issue calls out:

* **Digit-blind scan.** The var names include digits (``ATHENAEUM_REASONING_T1_
  MAX_TOKENS``). A ``[A-Z_]+`` regex truncates those to a fragment and undercounts,
  which is how the original 66/56 measurement missed four reasoning-tier vars.
  The scan here is ``ATHENAEUM_[A-Z0-9_]+``.
* **Empty any side.** If the ``src/`` scan, the docs scan, the scripts/hooks
  scan, or the derived-knob-set extraction (see below) comes back empty (a
  moved file, a broken glob, a renamed registry), the diff would be trivially
  empty and pass green. This check FAILS LOUDLY when any of them is empty.
* **Narrow scan surface (athenaeum#1376 class a).** ``ATHENAEUM_CLI`` and its
  siblings are read only by ``examples/claude-code/*.sh`` and ``scripts/``,
  outside the original ``src/``-only scan. Both directions now also scan those
  two surfaces — direction 1 still keys off ``src/`` alone (unchanged, see
  :func:`undocumented`), direction 2 treats the union as "read".
* **Runtime-constructed names (athenaeum#1376 class b).**
  ``athenaeum.provider.resolve_provider`` builds
  ``f"ATHENAEUM_{knob.upper()}_LLM_PROVIDER"`` at runtime — no literal token
  for e.g. ``ATHENAEUM_WRITE_LLM_PROVIDER`` exists anywhere in ``src/``, so a
  literal regex scan is structurally blind to it. This check expands the
  ``ATHENAEUM_<KNOB>_LLM_PROVIDER`` and ``ATHENAEUM_<KNOB>_THINKING``
  families from a knob set read live from
  :data:`athenaeum.prompt_registry.KNOBS` (see :func:`derive_knobs`) — not a
  hardcoded list — and counts those as read.

Both directions report a denominator even on success, so a green result is
evidence a sweep actually ran. Deliberately-internal / deliberately-unread
vars are listed in :data:`ALLOWLIST` / :data:`DOCS_TO_CODE_ALLOWLIST` with a
one-line reason each — never silently excluded from a scan. A
:data:`DOCS_TO_CODE_ALLOWLIST` entry that stops being necessary (the name
became read, or stopped being documented) is reported as stale rather than
silently kept (see :func:`stale_docs_to_code_allowlist`).

Run standalone (``python scripts/check_env_docs.py``) or via
``tests/test_env_docs.py`` (which is how CI gates it).

Exit codes:

* ``0`` — both directions clean.
* ``1`` — src -> docs: a var read by ``src/`` is undocumented (athenaeum#688,
  unchanged by athenaeum#1376).
* ``2`` — a scan side (``src/``, docs, ``scripts/``, the hooks directory, or
  the derived knob set) came back empty — loud misconfiguration, not a real
  zero.
* ``3`` — docs -> code: a documented var is read by no scanned surface, no
  family expansion, and no allowlist entry, OR a
  :data:`DOCS_TO_CODE_ALLOWLIST` entry is stale (athenaeum#1376).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / "src"
CONFIG_DOC = REPO_ROOT / "docs" / "configuration.md"
SCRIPTS_DIR = REPO_ROOT / "scripts"
HOOKS_DIR = REPO_ROOT / "examples" / "claude-code"

#: This file itself, excluded when scanning SCRIPTS_DIR (athenaeum#1376). Its
#: own docstring above wraps ``ATHENAEUM_REASONING_T1_MAX_TOKENS`` across a
#: line break, which a literal scan would harvest as the junk token
#: ``ATHENAEUM_REASONING_T1_``. Harmless either way — the widened surfaces
#: only ever EXCUSE a documented name, so a junk token can never suppress a
#: real report — but excluding this file keeps the "read" set to names
#: something genuinely reads, not names this checker's prose merely mentions.
_SELF = Path(__file__).resolve()

#: Digit-aware — the reasoning-tier vars carry a numeric tier (`T1`/`T2`), which a
#: `[A-Z_]+` scan silently truncates (issue athenaeum#688).
_ENV_RE = re.compile(r"ATHENAEUM_[A-Z0-9_]+")

#: Deliberately-internal / not-operator-facing vars that are intentionally NOT in
#: docs/configuration.md. Each MUST carry a one-line reason — never a silent
#: exclusion (issue athenaeum#688). Empty today: every ATHENAEUM_* read by src/ is
#: operator-facing and documented.
ALLOWLIST: dict[str, str] = {}

#: Documented vars that are intentionally read by nothing this check can see,
#: and are not covered by a family expansion — the docs->code mirror of
#: :data:`ALLOWLIST` (issue athenaeum#1376). Each entry MUST carry a one-line
#: reason. Empty today: the widened scan surfaces plus the family expansion
#: in :func:`expand_families` cover every documented ATHENAEUM_* name,
#: including ``ATHENAEUM_WRITE_THINKING`` (covered because ``write`` is a
#: real member of the derived knob set — see :func:`derive_knobs`).
DOCS_TO_CODE_ALLOWLIST: dict[str, str] = {}

#: Runtime-constructed env-var name templates (issue athenaeum#1376). Each is
#: built by an f-string at the call site (see
#: ``athenaeum.provider.resolve_provider`` / ``resolve_thinking``'s
#: per-knob convention), so no literal token for the expanded name ever
#: appears in src/ — expanded per knob in :func:`expand_families`.
_ENV_VAR_FAMILY_TEMPLATES: tuple[str, ...] = (
    "ATHENAEUM_{knob}_LLM_PROVIDER",
    "ATHENAEUM_{knob}_THINKING",
)


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


def scan_scripts(root: Path) -> set[str]:
    """Every ``ATHENAEUM_*`` name in ``root/**/*.py`` and ``root/**/*.sh``,
    excluding this checker's own file (see :data:`_SELF`)."""
    found: set[str] = set()
    paths = list(root.rglob("*.py")) + list(root.rglob("*.sh"))
    for path in paths:
        if path.resolve() == _SELF:
            continue
        found |= _scan(path.read_text(encoding="utf-8"))
    return found


def scan_hooks(root: Path) -> set[str]:
    """Every ``ATHENAEUM_*`` name in ``root/*.sh`` (the shipped Claude Code hooks)."""
    found: set[str] = set()
    for path in sorted(root.glob("*.sh")):
        found |= _scan(path.read_text(encoding="utf-8"))
    return found


def derive_knobs() -> set[str]:
    """The canonical per-knob model-routing namespace, read live from
    :data:`athenaeum.prompt_registry.KNOBS` (issue athenaeum#1376) — the same
    single source of truth the spend-ledger per-knob attribution and
    ``athenaeum.librarian._LIBRARIAN_ROUTED_KNOBS`` key off (see that
    constant's docstring in ``librarian.py``, and
    ``TestLibrarianRoutedKnobsDerivation`` in ``tests/test_pricing_config.py``,
    which pins the librarian-routed split against it). Deliberately NOT
    re-derived by grepping ``resolve_provider(...)`` / ``resolve_thinking(...)``
    call sites here: ``prompt_registry.KNOBS`` already IS "the knob set src/
    actually uses" and is the one place a ninth knob can't be added without
    something failing loudly too.
    """
    from athenaeum import prompt_registry

    return set(prompt_registry.KNOBS)


def expand_families(knobs: set[str]) -> set[str]:
    """Every concrete name in each :data:`_ENV_VAR_FAMILY_TEMPLATES` family for
    each *knob* (issue athenaeum#1376) — treated as read even though no
    literal token for it exists anywhere in ``src/``."""
    return {
        template.format(knob=knob.upper())
        for knob in knobs
        for template in _ENV_VAR_FAMILY_TEMPLATES
    }


def undocumented(src_vars: set[str], doc_vars: set[str]) -> set[str]:
    """Names read by src/ that are neither documented nor allowlisted.

    src -> docs direction (issue athenaeum#688). Unchanged by athenaeum#1376:
    keys off ``src_vars``/``doc_vars`` only, exactly as before — the widened
    scripts/hooks surfaces and the family expansion feed the docs -> code
    direction (:func:`unread`) only.
    """
    return src_vars - doc_vars - set(ALLOWLIST)


def unread(doc_vars: set[str], read_vars: set[str]) -> set[str]:
    """Names documented in docs/configuration.md that no scanned surface or
    family expansion reads, and that are not docs->code-allowlisted (issue
    athenaeum#1376). *read_vars* is the union of every surface this check
    treats as "read" — see :func:`main`.
    """
    return doc_vars - read_vars - set(DOCS_TO_CODE_ALLOWLIST)


def stale_docs_to_code_allowlist(doc_vars: set[str], read_vars: set[str]) -> set[str]:
    """:data:`DOCS_TO_CODE_ALLOWLIST` entries that no longer need to exist
    (issue athenaeum#1376): either the name is no longer documented, or some
    surface/family now reads it. A stale entry would otherwise sit forever
    excusing a name nothing needs excusing — this makes that rot visible
    instead of "not visible = presumed fine".
    """
    stale: set[str] = set()
    for name in DOCS_TO_CODE_ALLOWLIST:
        if name not in doc_vars or name in read_vars:
            stale.add(name)
    return stale


def main(argv: list[str] | None = None) -> int:
    src_vars = scan_tree(SRC_DIR)
    doc_vars = scan_docs(CONFIG_DOC)
    scripts_vars = scan_scripts(SCRIPTS_DIR)
    hooks_vars = scan_hooks(HOOKS_DIR)

    # Fail loud if any side is empty — a comparison that passes because a scan
    # matched nothing is the exact silent-pass class this check exists to
    # prevent.
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
    if not scripts_vars:
        print(
            f"env-docs: ERROR no ATHENAEUM_* vars found under {SCRIPTS_DIR} — the "
            "scripts scan is broken (moved scripts/? bad glob?); refusing to "
            "report a false pass.",
            file=sys.stderr,
        )
        return 2
    if not hooks_vars:
        print(
            f"env-docs: ERROR no ATHENAEUM_* vars found under {HOOKS_DIR} — the "
            "hooks scan is broken (moved examples/claude-code/? bad glob?); "
            "refusing to report a false pass.",
            file=sys.stderr,
        )
        return 2

    knobs = derive_knobs()
    if not knobs:
        print(
            "env-docs: ERROR athenaeum.prompt_registry.KNOBS came back empty — the "
            "knob derivation is broken (renamed registry? empty _META_ROWS?); "
            "refusing to report a false pass.",
            file=sys.stderr,
        )
        return 2

    # --- direction 1: src -> docs (issue athenaeum#688, unchanged) ---
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

    # --- direction 2: docs -> code (issue athenaeum#1376) ---
    families = expand_families(knobs)
    read_vars = src_vars | scripts_vars | hooks_vars | families
    unread_names = unread(doc_vars, read_vars)
    stale_allowlist = stale_docs_to_code_allowlist(doc_vars, read_vars)

    read_count = len(doc_vars & read_vars) + len(set(DOCS_TO_CODE_ALLOWLIST) & doc_vars)
    denom_reverse = (
        f"{read_count} read of {len(doc_vars)} ATHENAEUM_* vars documented in "
        f"{CONFIG_DOC} ({len(DOCS_TO_CODE_ALLOWLIST)} allowlisted)"
    )

    if unread_names or stale_allowlist:
        print(f"env-docs: FAIL (docs->code) — {denom_reverse}", file=sys.stderr)
        if unread_names:
            print(
                "env-docs: the following ATHENAEUM_* vars are documented in "
                "docs/configuration.md but are read by no scanned surface (src/, "
                "scripts/, examples/claude-code/) and covered by no known runtime "
                "family (add a reader, remove the doc entry, or allowlist with a "
                "reason in check_env_docs.py's DOCS_TO_CODE_ALLOWLIST):",
                file=sys.stderr,
            )
            for name in sorted(unread_names):
                print(f"  - {name}", file=sys.stderr)
        if stale_allowlist:
            print(
                "env-docs: the following DOCS_TO_CODE_ALLOWLIST entries are stale "
                "(no longer documented, or now read by a scanned surface/family) — "
                "remove them:",
                file=sys.stderr,
            )
            for name in sorted(stale_allowlist):
                print(f"  - {name}", file=sys.stderr)
        return 3

    print(f"env-docs: OK (docs->code) — {denom_reverse}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
