# SPDX-License-Identifier: Apache-2.0
"""Issue athenaeum#2421 — the cross-test ``ATHENAEUM_LLM_PROVIDER`` leak.

Two tests from athenaeum#2367 failed once in CI and passed on a re-run with no
code change: the config on disk said ``llm.provider: claude-cli`` and
``resolve_provider`` still returned ``api``, and ``athenaeum init`` disclosed no
provider at all. Both symptoms are what a leaked ``ATHENAEUM_LLM_PROVIDER=api``
looks like, because that variable is the FIRST tier of
:func:`athenaeum.provider._resolve_global_provider`'s chain and so outranks the
yaml the test itself wrote.

The leaker is :func:`athenaeum.drain.run_drain`, which assigns
``os.environ["ATHENAEUM_LLM_PROVIDER"] = "api"`` to force the batch transport
and used to leave it set. Out of process that is harmless; in process it
contaminates every later test on the same xdist worker.
``tests/test_drain.py`` calls it behind an autouse snapshot/restore fixture, but
``tests/test_librarian_run_refusal.py::TestDrainUnaffected`` calls it with no
such guard -- and at the merge ref of the failing run that module and the
athenaeum#2367 module were assigned to the SAME shard (shard 2, the job that
failed), while the guarded ``test_drain.py`` was on shard 1.

The fix is in ``run_drain`` itself (``drain._env_restored``); its unit coverage,
including both error paths, lives in ``tests/test_drain.py``. This module holds
the end-to-end regression proof: replay the leaker and the two victims in ONE
process, in a fixed order.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

#: Node-ids below are repo-root-relative, so the replay subprocess is pinned to
#: the repo root rather than inheriting whatever cwd the outer run has.
_REPO_ROOT = Path(__file__).resolve().parent.parent

#: The leaker, then the probe that fails if the leak survived it, then the two
#: tests that actually failed in CI. Order is the point: pytest runs explicit
#: node-id arguments in the order given, all in one process.
_LEAKER = (
    "tests/test_librarian_run_refusal.py::TestDrainUnaffected"
    "::test_drain_zero_progress_still_refuses_loudly"
)
_PROBE = (
    "tests/test_2421_provider_env_leak.py::TestAmbientProviderEnv"
    "::test_no_earlier_test_left_the_provider_env_set"
)
_VICTIMS = (
    "tests/test_2367_claude_cli_first_recall.py"
    "::TestDefaultProviderSelectionOnFreshRoot"
    "::test_no_key_and_usable_claude_binary_selects_claude_cli",
    "tests/test_2367_claude_cli_first_recall.py::TestInitDisclosesProvider"
    "::test_init_names_claude_cli_and_the_plan_draw",
)


class TestAmbientProviderEnv:
    def test_no_earlier_test_left_the_provider_env_set(self) -> None:
        """No test earlier in THIS process left the provider env behind.

        Trivially true when run on its own, which is how the normal suite runs
        it. It is load-bearing in the ordered replay below, where it runs
        directly after the unguarded ``run_drain`` caller: without the
        ``run_drain`` fix this assertion is the one that fails, so the replay
        tests the source fix rather than only the victims' own isolation
        fixture.
        """
        assert "ATHENAEUM_LLM_PROVIDER" not in os.environ


class TestOrderedReplayOfTheFailingShard:
    def test_the_two_2367_tests_pass_after_the_leaker(self) -> None:
        """The acceptance criterion: leaker then victims, one process, in order.

        A subprocess rather than an in-process re-entry: pytest is not
        re-entrant, and this is also the only way to pin the ORDER
        independently of whatever shard or worker the outer run happens to be.
        """
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-p",
                "no:cacheprovider",
                "-p",
                "no:xdist",
                "--no-header",
                "-q",
                _LEAKER,
                _PROBE,
                *_VICTIMS,
            ],
            capture_output=True,
            text=True,
            timeout=600,
            cwd=_REPO_ROOT,
        )
        assert completed.returncode == 0, (
            f"ordered replay failed (rc={completed.returncode})\n"
            f"--- stdout ---\n{completed.stdout}\n--- stderr ---\n{completed.stderr}"
        )
        # Guard against a vacuous pass: a mistyped node-id makes pytest exit
        # 4 (usage error), but a `--deselect`-shaped miscollection could in
        # principle still exit 0 having run nothing.
        assert "4 passed" in completed.stdout, completed.stdout


class TestTheLeakerIsStillTheOnlyUnguardedCaller:
    def test_run_drain_restores_the_forced_provider(self) -> None:
        """Belt and braces at the module boundary: the fix is a property of
        ``run_drain``, so it holds for ANY caller, guarded or not -- which is
        what makes the guard in ``tests/test_drain.py`` redundant rather than
        load-bearing, and what keeps the next unguarded caller harmless.
        """
        from athenaeum import drain

        assert hasattr(drain, "_env_restored")
        assert drain.PROVIDER_ENV == "ATHENAEUM_LLM_PROVIDER"
        with pytest.raises(RuntimeError, match="boom"):
            with drain._env_restored(drain.PROVIDER_ENV):
                os.environ[drain.PROVIDER_ENV] = "api"
                raise RuntimeError("boom")
        assert drain.PROVIDER_ENV not in os.environ
