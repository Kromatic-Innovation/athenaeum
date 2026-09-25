# SPDX-License-Identifier: Apache-2.0
"""Issue athenaeum#1887: the rollout eval harness's ``UserPromptSubmit`` hook
default must point at the packaged adapter console script
(``athenaeum-claude-hook``, :mod:`athenaeum.claude_code_adapter`), not the
retired shell hook (``examples/claude-code/user-prompt-recall.sh``) --
carried out of issue athenaeum#1361 as the one acceptance criterion that
issue's own eval-fidelity gap left unclosed.

Fully offline: no subprocess is spawned, no corpus is materialized. NOT
``rollout``-marked (issue athenaeum#1742's rule for a module that imports
``tests.evals.rollout`` but spends no token and spawns nothing) -- runs in
the default selection.

Both tests exercise :func:`tests.evals.rollout.resolve_user_prompt_hook`
directly against a monkeypatched environment -- NOT
``tests.evals.rollout.USER_PROMPT_HOOK``, which is a module-level snapshot
resolved once at import time (see that constant's own docstring) and would
not observe a test's env change made after import.
"""

from __future__ import annotations

import pytest

from tests.evals.rollout import SHELL_USER_PROMPT_HOOK, resolve_user_prompt_hook


def test_default_hook_resolves_to_the_packaged_adapter_not_the_shell_script(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC3: with ``ATHENAEUM_EVAL_HOOK`` unset, the resolved hook is the
    packaged adapter console script -- named ``athenaeum-claude-hook`` -- and
    is explicitly NOT the retired shell hook."""
    monkeypatch.delenv("ATHENAEUM_EVAL_HOOK", raising=False)

    resolved = resolve_user_prompt_hook()

    assert resolved.name == "athenaeum-claude-hook"
    assert resolved != SHELL_USER_PROMPT_HOOK
    assert resolved.name != "user-prompt-recall.sh"


def test_eval_hook_shell_flag_selects_the_retired_shell_script(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one-release escape hatch: ``ATHENAEUM_EVAL_HOOK=shell`` selects
    ``examples/claude-code/user-prompt-recall.sh`` -- the exact path
    :data:`tests.evals.rollout.SHELL_USER_PROMPT_HOOK` names -- so the two
    hooks can still be compared side by side in this harness for one
    release (never a request to delete the shell implementation -- that is
    issue athenaeum#1363, explicitly out of scope here)."""
    monkeypatch.setenv("ATHENAEUM_EVAL_HOOK", "shell")

    resolved = resolve_user_prompt_hook()

    assert resolved == SHELL_USER_PROMPT_HOOK
    assert resolved.name == "user-prompt-recall.sh"


def test_eval_hook_flag_is_exact_match_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any value other than the exact string ``"shell"`` falls through to
    the adapter default, mirroring the fail-safe-toward-the-live-path shape
    this harness uses elsewhere (e.g. the adapter's own ``AUTO_RECALL``
    gate) -- a typo in the escape hatch can never silently mis-measure the
    wrong hook as if it were the default."""
    monkeypatch.setenv("ATHENAEUM_EVAL_HOOK", "Shell")

    resolved = resolve_user_prompt_hook()

    assert resolved.name == "athenaeum-claude-hook"
