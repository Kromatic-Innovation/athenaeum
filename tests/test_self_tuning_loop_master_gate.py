# SPDX-License-Identifier: Apache-2.0
"""Self-tuning loop master-gate CENSUS guard (issue athenaeum#719 AC11/AC15,
athenaeum#2020's own note that this test "will flip to a real pass (and must
then be updated) the moment either sibling's own nightly phase lands gated
the same way" -- see `CHANGELOG.md`'s athenaeum#2020 entry).

This file used to be a deliberately failing `xfail(strict=True)` TODO guard
documenting that `tier_movement_proposals.py` (athenaeum#2019) and
`auto_apply_proposals.py` (athenaeum#2018) shipped drafters with no nightly
wiring of their own. Both are now wired into `librarian.run()` as
`_run_tier_movement_proposals_phase` / `_run_auto_apply_proposals_phase`,
gated by the SAME master key (`resolve_signal_mining_enabled`)
`_run_signal_mining_phase` already used for `dimension_proposals.py` -- so
this is now a real, passing CENSUS guard rather than a TODO.

What it checks:

1. For each sibling wired through its own `_run_<module>_phase` function
   (`tier_movement_proposals`, `auto_apply_proposals`): that function
   exists on `athenaeum.librarian`, its source consults
   `resolve_signal_mining_enabled`, and the module's own name appears
   somewhere in `librarian.py`'s source (belt-and-suspenders that it is
   actually imported/called, not just referenced in a docstring).
2. `dimension_proposals` -- the master key's ORIGINAL subject -- is wired
   through `_run_signal_mining_phase` directly (there is no
   `_run_dimension_proposals_phase`), so it gets the same three checks
   against that function by name instead.
3. An explicit, documented exclusion table for `*_proposals` modules that
   are deliberately NOT part of the nightly run, each with a one-line
   reason (see `_EXCLUDED_PROPOSAL_MODULES` below).
4. A census: every `src/athenaeum/*_proposals.py` module discovered by
   globbing at test time must appear in EXACTLY ONE of the wired set or
   the excluded table -- so a newly-added `*_proposals` module can never
   silently join the unwired set without failing this test.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import textwrap
from pathlib import Path

import pytest

import athenaeum
import athenaeum.librarian as librarian_mod

# Sibling "*_proposals" modules wired into the nightly run via their OWN
# `_run_<module>_phase` function, gated by the self-tuning loop's master key
# (`resolve_signal_mining_enabled`). `dimension_proposals` is NOT listed
# here -- it is wired through `_run_signal_mining_phase` directly (see
# `_DIMENSION_PROPOSALS_PHASE_FN_NAME` below) and is checked separately.
_WIRED_SIBLING_PROPOSAL_MODULES: tuple[str, ...] = (
    "tier_movement_proposals",
    "auto_apply_proposals",
)

# `dimension_proposals` is the master key's original subject (issue
# athenaeum#2020): wired through `_run_signal_mining_phase`, not a
# `_run_dimension_proposals_phase` of its own.
_DIMENSION_PROPOSALS_MODULE = "dimension_proposals"
_DIMENSION_PROPOSALS_PHASE_FN_NAME = "_run_signal_mining_phase"

# `*_proposals` modules deliberately NOT scheduled by the nightly run at
# all -- each reason is load-bearing; this is not a "not yet done" list.
_EXCLUDED_PROPOSAL_MODULES: dict[str, str] = {
    # The detector consumes `page_decompose.DecomposeReport`s, and nothing
    # in the nightly run produces them; scheduling it would require
    # deciding the cost of decomposing every page nightly, which is an
    # open product decision tracked by the follow-up issue.
    "page_split_proposals": (
        "detector consumes page_decompose.DecomposeReport objects, which no "
        "nightly phase produces; scheduling it means deciding the cost of "
        "decomposing every page nightly -- an open product decision, not a "
        "missing line of wiring"
    ),
    # Has no detector BY DESIGN -- the module's own docstring states "No
    # detector exists yet" and the drafter only takes caller-supplied
    # candidates. There is nothing to schedule.
    "policy_pack_edit_proposals": (
        "no detector by design; drafter takes caller-supplied candidates only"
    ),
    # A different epic (athenaeum#1063), gated by its OWN master key
    # (`resolve_rule_proposals_enabled`) -- never part of the self-tuning
    # loop (athenaeum#719) `resolve_signal_mining_enabled` gates.
    "rule_proposals": (
        "different epic (athenaeum#1063), own master key "
        "(resolve_rule_proposals_enabled), not part of athenaeum#719's loop"
    ),
}


def _gate_code(fn: object) -> str:
    """The function's SOURCE with its docstring and comment lines removed.

    A bare substring check over ``inspect.getsource`` is satisfied by the
    phase's own DOCSTRING naming the master key -- verified by mutation:
    replacing the real ``if not resolve_signal_mining_enabled(...)`` guard
    with ``if False:`` left the docstring mention in place and the check
    still passed. Strip prose first so this guard reads executable code only
    (the same reason a workflow-structure assertion must slice out the YAML
    key it is checking rather than grep the whole file).
    """
    src = inspect.getsource(fn)  # type: ignore[arg-type]
    tree = ast.parse(textwrap.dedent(src))
    node = tree.body[0]
    assert isinstance(node, ast.FunctionDef)
    body = node.body
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    # ast.unparse drops comments as well as the docstring.
    return "\n".join(ast.unparse(stmt) for stmt in body)


def _discover_proposal_modules() -> set[str]:
    """Every `*_proposals` module under `src/athenaeum/` -- globbed at test
    time so a newly-added module cannot silently dodge this census."""
    pkg_dir = Path(athenaeum.__file__).parent
    return {p.stem for p in pkg_dir.glob("*_proposals.py")}


class TestWiredSiblings:
    @pytest.mark.parametrize("module_name", _WIRED_SIBLING_PROPOSAL_MODULES)
    def test_master_key_gates_phase(self, module_name: str) -> None:
        # The sibling module must actually exist -- importing raises loudly
        # instead of silently no-oping if it does not.
        importlib.import_module(f"athenaeum.{module_name}")

        phase_fn_name = f"_run_{module_name}_phase"
        assert hasattr(librarian_mod, phase_fn_name), (
            f"expected librarian.{phase_fn_name} to exist, wired into run(), "
            "and gated by resolve_signal_mining_enabled"
        )
        phase_source = _gate_code(getattr(librarian_mod, phase_fn_name))
        assert "resolve_signal_mining_enabled" in phase_source, (
            f"librarian.{phase_fn_name} exists but does not consult the "
            "self-tuning loop's master key"
        )
        # Belt-and-suspenders: the module's own name should also appear
        # somewhere in librarian.py now that it is genuinely wired in.
        source = inspect.getsource(librarian_mod)
        assert module_name in source


class TestDimensionProposalsOriginalSubject:
    def test_master_key_gates_signal_mining_phase(self) -> None:
        importlib.import_module(f"athenaeum.{_DIMENSION_PROPOSALS_MODULE}")
        assert hasattr(librarian_mod, _DIMENSION_PROPOSALS_PHASE_FN_NAME)
        phase_source = _gate_code(
            getattr(librarian_mod, _DIMENSION_PROPOSALS_PHASE_FN_NAME)
        )
        assert "resolve_signal_mining_enabled" in phase_source
        source = inspect.getsource(librarian_mod)
        assert _DIMENSION_PROPOSALS_MODULE in source


class TestCensusIsExhaustive:
    def test_every_proposals_module_is_wired_or_excluded(self) -> None:
        discovered = _discover_proposal_modules()
        wired = {_DIMENSION_PROPOSALS_MODULE, *_WIRED_SIBLING_PROPOSAL_MODULES}
        excluded = set(_EXCLUDED_PROPOSAL_MODULES)

        # No module should be double-booked as both wired and excluded.
        assert wired & excluded == set(), (
            f"module(s) {wired & excluded} listed as both wired and excluded"
        )

        covered = wired | excluded
        missing = discovered - covered
        assert not missing, (
            f"newly discovered `*_proposals` module(s) {missing} are neither "
            "wired into the nightly run nor in the documented exclusion "
            "table -- add a `_run_<module>_phase` gated by "
            "resolve_signal_mining_enabled, or add a documented reason to "
            "_EXCLUDED_PROPOSAL_MODULES"
        )
        stale = covered - discovered
        assert not stale, (
            f"{stale} is listed as wired/excluded but no longer exists under "
            "src/athenaeum/ -- remove the stale entry"
        )

    def test_exclusion_reasons_are_substantive(self) -> None:
        """An exclusion must carry a real reason, not a placeholder.

        The whole point of the table is that each entry is load-bearing --
        a ``TODO``/``FIXME`` stub would turn this census back into the
        silent not-yet-done list it replaced.
        """
        for module_name, reason in _EXCLUDED_PROPOSAL_MODULES.items():
            text = reason.strip()
            assert text, f"{module_name} has an empty exclusion reason"
            assert len(text) >= 40, (
                f"{module_name}'s exclusion reason is too short to be a real "
                f"reason: {text!r}"
            )
            for stub in ("todo", "fixme", "tbd", "xxx"):
                assert stub not in text.lower(), (
                    f"{module_name}'s exclusion reason is a {stub.upper()} "
                    "placeholder, not a reason"
                )
