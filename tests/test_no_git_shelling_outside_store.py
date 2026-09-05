# SPDX-License-Identifier: Apache-2.0
"""Mechanical guard: the ``git_snapshot`` whole-tree-commit primitive does not
reappear duplicated outside :class:`athenaeum.store.FilesystemStore`, and the
KNOWN set of remaining knowledge-store git-argv call sites does not grow
(issue athenaeum#978, slice S3, AC6; design note
``docs/extending/whole-store-adapter-design.md`` §4.2 / §4.4 / §9.2's S3 row).

**What this scans, precisely:** every ``src/athenaeum/*.py`` module (i.e. only
real, non-test, non-``.github`` source — the AST walk below parses each file
as Python and only ever inspects genuine list/tuple literal AST nodes, so a
comment, a docstring, or the string ``"git"`` inside an unrelated string
literal can never trip either assertion here; there is no text ``grep``
anywhere in this module). ``tests/`` and ``.github/`` are outside the glob
entirely, not merely excluded after the fact.

**Why this is a baseline, not a zero-tolerance guard (unlike
``test_import_graph_acyclic.py``'s ``ALLOWED_SCCS = []``):** design note §4.2
inventories the knowledge-store git usage as TEN call sites across EIGHT
modules (``librarian.py`` twice, plus ``rules.py``/``retire.py``/
``auto_memory_prune.py``/``filename_entity_prune.py``/``memory_index.py``/
``corrections.py``/``init.py``/``status.py``); a mechanical AST sweep (see
``KNOWN_GIT_ARGV_MODULES`` below) finds two more the design note's own table
missed (``decay_sweep.py``, ``pending_merges.py``) and resolves ``librarian.py``
to three sites, not two (``git_push``/``git_pull``/``_capture_head``'s
``rev-parse``) once ``git_snapshot`` — the ONE site this slice's Plan actually
names — is subtracted. S3's five-step Plan (the issue body) is "relocate
``git_snapshot``'s body onto the store, migrate its two call sites [found to
be four — see the store.py module docstring / this slice's completion report],
and convert the four Tier-A gates plus two Tier-B fallbacks to
``capabilities.versioned`` checks." It does NOT propose migrating
push/pull/rev-parse/``git init``/``git log``/the scoped retire-and-prune
commits themselves onto the store — the ``Store`` protocol (S1, athenaeum#976)
has no scoped-commit or push/pull primitive to migrate them onto, and adding
one would be new protocol surface a sibling lane (S7, athenaeum#982) is
consuming concurrently off the same base. Reading AC6 ("no git-shelling code
remains outside FilesystemStore") to require migrating all eleven of those
sites in this slice would be scope well beyond S3's own Plan and the
"ADDITIVE and minimal" edit-scope this lane was briefed under — so this test
enforces the part of AC6 that IS this slice's job (the snapshot primitive
specifically does not get duplicated) as a hard, zero-tolerance assertion, and
tracks the rest as an explicit, monotonically-shrinking baseline: a count
going DOWN (a future slice migrates a module fully) is welcome and just needs
the baseline edited down; a count going UP, or an unlisted module appearing,
is the regression this guard exists to catch — a NEW git-shelling site landed
outside ``FilesystemStore`` without going through it.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src" / "athenaeum"

#: Modules that shell out to git for ATHENAEUM'S OWN repository (deploy-SHA
#: stamping, version reporting) — design note §4.2's right-hand column, a
#: DIFFERENT git working tree from the knowledge store this design governs:
#: "They never intersect in code ... this design touches only the left
#: column." Exempt from the knowledge-store inventory below; migrating them
#: onto athenaeum.store would be a category error, not a missed site.
OWN_REPO_GIT_MODULES = frozenset(
    {
        "backlog_price_sheet",
        "ordinary_night_table",
        "push_metrics",
        "shadow_linkage",
    }
)

#: The knowledge-store git-argv list-literal sites this slice did NOT migrate,
#: pinned as {module_stem: count}. See the module docstring for what each
#: module's sites are (push/pull/rev-parse for librarian.py; each module's own
#: scoped ``git rm``-then-commit retirement mechanics for the rest) and why
#: leaving them as direct ``subprocess`` calls is in scope for a LATER slice,
#: not this one. ``store.py`` itself is exempt by construction — the whole
#: point is that its git-argv literals (``git status --porcelain`` / ``git add
#: -A`` / ``git commit`` / ``git rev-parse HEAD`` in
#: :meth:`~athenaeum.store.FilesystemStore.snapshot`) are the ONE place this
#: is now allowed to live.
KNOWN_GIT_ARGV_MODULES: dict[str, int] = {
    "auto_memory_prune": 1,
    "corrections": 1,
    "decay_sweep": 1,
    "filename_entity_prune": 1,
    "init": 3,
    "librarian": 3,  # git_push, git_pull, _capture_head's rev-parse — NOT snapshot
    "memory_index": 1,
    "pending_merges": 2,
    "pii_restore": 1,  # athenaeum#1037: reads the knowledge store's OWN history
    # (git log --follow / git show) to locate a marker's pre-image and to
    # resolve a retro filename by timestamp key — read-only history lookups,
    # not a snapshot/commit, so there is no FilesystemStore primitive this
    # maps onto.
    "reconcile": 1,  # athenaeum#1143: `git show <commit>:<path>` byte-identity
    # checks (pii_restore's read-only-history shape) PLUS a scoped `git rm`
    # + commit retirement of reconciled raw files (retire.py's shape) — both
    # go through the SAME single `_git` helper, so this is one AST literal
    # site, not two.
    "retire": 1,
    "rules": 1,
    "status": 1,
}


def _is_git_argv_literal(node: ast.AST) -> bool:
    """True for a ``["git", ...]`` / ``("git", ...)`` literal — a real AST
    list/tuple node whose first element is the string constant ``"git"``.
    Never matches a comment, docstring, or an unrelated string containing the
    substring ``git``."""
    if not isinstance(node, (ast.List, ast.Tuple)) or not node.elts:
        return False
    head = node.elts[0]
    return isinstance(head, ast.Constant) and head.value == "git"


def _git_argv_lines(path: Path) -> list[int]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return sorted(
        {node.lineno for node in ast.walk(tree) if _is_git_argv_literal(node)}
    )


def test_git_snapshot_is_not_redefined_or_duplicated_outside_the_store() -> None:
    """issue athenaeum#978 AC1/AC2: ``git_snapshot`` (the whole-tree ``git
    status --porcelain`` / ``git add -A`` / ``git commit`` sequence) was
    MOVED from ``librarian.py`` to ``FilesystemStore.snapshot`` — moved, not
    copied. Two mechanical checks, over every module except ``store.py``:

    1. No module defines a function literally named ``git_snapshot``.
    2. No module contains the exact ``["git", "add", "-A"]`` whole-tree
       staging literal that was ``git_snapshot``'s signature move — the
       shape D5 / this slice's AC2 rationale calls "forking the seam at the
       snapshot primitive."
    """
    for path in sorted(SRC.glob("*.py")):
        if path.stem == "store":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

        redefined = [
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "git_snapshot"
        ]
        assert not redefined, (
            f"{path.name}:{redefined[0]} redefines git_snapshot — it was "
            "MOVED to FilesystemStore.snapshot (issue athenaeum#978), not "
            "left behind or duplicated"
        )

        for node in ast.walk(tree):
            if not _is_git_argv_literal(node) or len(node.elts) < 3:
                continue
            second, third = node.elts[1], node.elts[2]
            is_add_dash_a = (
                isinstance(second, ast.Constant)
                and second.value == "add"
                and isinstance(third, ast.Constant)
                and third.value == "-A"
            )
            assert not is_add_dash_a, (
                f"{path.name}:{node.lineno} — a `git add -A` whole-tree "
                "stage literal reappeared outside FilesystemStore; this is "
                "git_snapshot's signature and must live only in "
                "FilesystemStore.snapshot() (issue athenaeum#978)"
            )


def test_git_argv_sites_outside_store_do_not_exceed_the_pinned_baseline() -> None:
    """The remaining knowledge-store git-argv sites (everything except the
    now-migrated snapshot primitive, checked separately above, and
    athenaeum's-own-repo git usage, a different tree entirely) do not exceed
    the pinned baseline. See the module docstring for what "baseline, not
    zero" means here and why."""
    found: dict[str, int] = {}
    for path in sorted(SRC.glob("*.py")):
        module = path.stem
        if module in ("store", "__init__") or module in OWN_REPO_GIT_MODULES:
            continue
        count = len(_git_argv_lines(path))
        if count:
            found[module] = count

    assert found == KNOWN_GIT_ARGV_MODULES, (
        "knowledge-store git-argv call sites outside FilesystemStore "
        f"changed.\nfound={found}\nbaseline={KNOWN_GIT_ARGV_MODULES}\n"
        "A count going DOWN (a module fully migrated onto athenaeum.store) "
        "is welcome — lower the baseline to match. A count going UP, or an "
        "unlisted module appearing, is a regression: a NEW git-shelling "
        "site landed outside athenaeum.store.FilesystemStore (design note "
        "§4.4) without an accompanying store primitive to route it through."
    )
