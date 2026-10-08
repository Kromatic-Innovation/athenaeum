# SPDX-License-Identifier: Apache-2.0
"""TODO guard (issue athenaeum#2020 "DO" note): the self-tuning loop's master
key (``resolve_signal_mining_enabled``) is supposed to gate every
``*_proposals`` module's nightly phase, exactly as it already gates
``dimension_proposals.py`` via ``librarian._run_signal_mining_phase``.

On this branch's base, two sibling proposal-rail modules landed WITHOUT any
nightly-phase wiring of their own at all (issue athenaeum#2019's
``tier_movement_proposals.py`` / ``policy_pack_edit_proposals.py`` — merged
into this branch's base ahead of this child, per the Occam dispatch note),
let alone wiring threaded through this epic's master key. This test is a
deliberately failing TODO (``xfail(strict=True)``) pinned to flip to a real
pass the moment either sibling's own nightly wiring lands and is gated the
same way — never silently XPASS, which would mean this guard quietly
stopped checking anything.

``rule_proposals.py`` is excluded: issue athenaeum#1063's own master key
(``resolve_rule_proposals_enabled``) is a different epic, never part of the
self-tuning loop (issue athenaeum#719) this master key gates — mirroring
the naming distinction `_run_signal_mining_phase`'s own docstring draws.
"""

from __future__ import annotations

import importlib
import inspect

import pytest

import athenaeum.librarian as librarian_mod

# Every "*_proposals" module under src/athenaeum that is part of THIS epic's
# self-tuning loop and is NOT yet gated by resolve_signal_mining_enabled.
# Update this tuple (removing an entry) as each sibling's nightly wiring
# lands gated by the master key -- never add an entry just to silence a
# failure.
_UNGATED_SIBLING_PROPOSAL_MODULES: tuple[str, ...] = (
    "tier_movement_proposals",
    "policy_pack_edit_proposals",
)


@pytest.mark.xfail(
    strict=True,
    reason=(
        "athenaeum#2020 TODO: thread resolve_signal_mining_enabled into "
        "whichever nightly phase eventually drives "
        "tier_movement_proposals.py / policy_pack_edit_proposals.py "
        "(issue athenaeum#2019), then drop this xfail and remove the "
        "corresponding entries from _UNGATED_SIBLING_PROPOSAL_MODULES."
    ),
)
def test_master_key_gates_every_sibling_proposals_module() -> None:
    source = inspect.getsource(librarian_mod)
    for module_name in _UNGATED_SIBLING_PROPOSAL_MODULES:
        # The sibling module must actually exist on this base -- if it does
        # not, there is nothing to gate yet and asserting against it would
        # be vacuous; importing raises loudly instead of silently no-oping.
        importlib.import_module(f"athenaeum.{module_name}")

        phase_fn_name = f"_run_{module_name}_phase"
        assert hasattr(librarian_mod, phase_fn_name), (
            f"expected librarian.{phase_fn_name} to exist, wired into run(), "
            "and gated by resolve_signal_mining_enabled"
        )
        phase_source = inspect.getsource(getattr(librarian_mod, phase_fn_name))
        assert "resolve_signal_mining_enabled" in phase_source, (
            f"librarian.{phase_fn_name} exists but does not consult the "
            "self-tuning loop's master key"
        )
        # Belt-and-suspenders: the module's own name should also appear
        # somewhere in librarian.py once it is genuinely wired in.
        assert module_name in source
