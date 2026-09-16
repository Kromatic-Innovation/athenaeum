# SPDX-License-Identifier: Apache-2.0
"""athenaeum#1654 / athenaeum#1686: the attachment eval's aggregate-floor test
carries an ``AssertionError``-scoped xfail marker so a red Live-API eval job
(the expected state while ``ATTACHMENT_FLOOR`` is unmet, per athenaeum#1580
AC3) does not silently mask an unrelated regression.

This test is DELIBERATELY unmarked (no ``eval``/``live``/``embedding``) so it
runs in the default pytest selection, unlike
``tests/evals/test_attachment_eval.py`` itself (module-level
``pytestmark = pytest.mark.eval``, deselected by default). It never invokes
the eval -- it only imports the module and inspects the marker object on
:func:`tests.evals.test_attachment_eval.test_attachment_aggregate_floor`, so
a future edit that removes the marker outright, or widens ``raises`` past
``AssertionError``, fails THIS test instead of silently letting the job go
green for the wrong reason.

**The ``strict`` assertion is inverted from athenaeum#1654 on purpose.** That
issue pinned ``strict=True`` so an unexpected pass would red the job and force
the marker's removal the day a fix cleared the floor. The premise -- that an
XPASS means the librarian improved -- was falsified within a day:
``test_attachment_aggregate_floor`` read XFAIL on six consecutive main-push
Evals runs (35003616401, 35004869331, 35006866272, 35009922756, 35027458483,
35037261767) and then FAILED with ``[XPASS(strict)]`` on the seventh
(35042967267), with no attach-vs-mint routing change anywhere in that window.
The score is a live-API measurement that varies around the floor, so
``strict=True`` reds a main-push job on a lucky run and files a maintenance
issue for it (athenaeum#1686). This guard therefore pins ``strict`` to
``False`` -- the marker must NOT be re-strictened without a noise-robust
signal to replace it -- while keeping the other two invariants athenaeum#1654
erected, which the variance finding does not touch.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from tests.evals import test_attachment_eval as attachment_eval_module


def test_attachment_aggregate_floor_carries_scoped_xfail() -> None:
    target = attachment_eval_module.test_attachment_aggregate_floor
    xfail_marks = [
        mark for mark in getattr(target, "pytestmark", []) if mark.name == "xfail"
    ]
    assert len(xfail_marks) == 1, (
        "test_attachment_aggregate_floor must carry exactly one "
        f"pytest.mark.xfail; found {len(xfail_marks)}"
    )
    mark = xfail_marks[0]
    assert mark.kwargs.get("raises") is AssertionError, (
        "the aggregate-floor xfail must be scoped to raises=AssertionError, "
        "or an infrastructure error (e.g. a crashed tier call) would be "
        "swallowed as an 'expected' failure instead of surfacing"
    )


def test_attachment_aggregate_floor_xfail_is_not_strict() -> None:
    """The marker must stay non-strict (athenaeum#1686).

    Under ``strict=True`` an unexpected pass fails the run. The attachment
    score is a live-API measurement that crossed the floor once in seven
    main-push runs with no routing change, so re-strictening this marker
    re-arms a red main-push job on run-to-run variance. Replace it with a
    signal variance cannot trigger (several consecutive at-or-above-floor
    runs) before pinning ``strict`` back to ``True``.
    """
    target = attachment_eval_module.test_attachment_aggregate_floor
    mark = next(
        mark for mark in getattr(target, "pytestmark", []) if mark.name == "xfail"
    )
    assert mark.kwargs.get("strict") is False, (
        "the aggregate-floor xfail must be strict=False: the layer's score "
        "varies run to run around ATTACHMENT_FLOOR, so a strict marker reds "
        "a main-push Evals job on a lucky run rather than on a fix "
        "(athenaeum#1686)"
    )


def test_attachment_floor_value_is_unchanged() -> None:
    """The floor stays aspirational (athenaeum#1580 AC3, athenaeum#1654).

    Non-strict xfail is not a licence to tune the floor to observed
    behaviour -- that is the rubber stamp the module docstring warns against.
    It is only a licence to stop reporting variance as a job failure.
    """
    assert attachment_eval_module.ATTACHMENT_FLOOR == 4


class TestTheMarkerBehavesAsClaimed:
    """Synthesize the XPASS, rather than wait ~1 main-push run in 7 for it.

    The two tests above inspect marker *kwargs*. That is not the claim the
    change rests on -- the claim is about what pytest DOES when the
    aggregate-floor assertion happens to hold on a lucky live-API run, which
    is a rare condition nothing in the default suite ever reaches. So run a
    real pytest over a throwaway module carrying the production marker's exact
    kwargs, once with a body that passes and once with a body that raises
    ``AssertionError``, and assert the process exit status directly.
    """

    @staticmethod
    def _production_marker_kwargs() -> str:
        """Mirror the REAL marker, so these tests cannot drift away from it.

        Rendering a hand-copied ``strict=False, raises=AssertionError`` here
        would let the production marker be re-strictened while these tests
        went on proving a string literal behaves correctly.
        """
        target = attachment_eval_module.test_attachment_aggregate_floor
        mark = next(
            mark for mark in getattr(target, "pytestmark", []) if mark.name == "xfail"
        )
        raises = mark.kwargs["raises"]
        return (
            f"strict={mark.kwargs['strict']!r}, "
            f"raises={raises.__name__}, "
            "reason='synthesized from the production marker'"
        )

    def _run(self, tmp_path: Path, body: str) -> subprocess.CompletedProcess[str]:
        module = tmp_path / "test_synthetic_floor.py"
        module.write_text(
            "import pytest\n\n\n"
            f"@pytest.mark.xfail({self._production_marker_kwargs()})\n"
            "def test_floor() -> None:\n"
            f"    {body}\n",
            encoding="utf-8",
        )
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                str(module),
                "-q",
                "-o",
                "addopts=",
                "-p",
                "no:cacheprovider",
            ],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_an_unexpected_pass_does_not_fail_the_run(self, tmp_path: Path) -> None:
        """The XPASS path -- the one athenaeum#1686 is about.

        Run 35042967267 reported ``FAILED ... [XPASS(strict)]`` and reddened a
        main-push Evals job without any routing fix having landed. Under the
        non-strict marker the same outcome must leave the run green.
        """
        result = self._run(tmp_path, "assert True")
        assert result.returncode == 0, (
            "a non-strict xfail that unexpectedly passes must NOT fail the "
            f"run; pytest exited {result.returncode}\n{result.stdout}"
        )
        assert "xpassed" in result.stdout, (
            "the unexpected pass must still be REPORTED as an xpass -- the "
            "point is to stop failing the job on it, not to hide it\n"
            f"{result.stdout}"
        )

    def test_the_expected_failure_is_still_absorbed(self, tmp_path: Path) -> None:
        """The XFAIL path -- what athenaeum#1654 bought, and keeps."""
        result = self._run(tmp_path, "raise AssertionError('below floor')")
        assert result.returncode == 0, (
            "the expected below-floor assertion must still be absorbed as an "
            f"xfail; pytest exited {result.returncode}\n{result.stdout}"
        )
        assert "xfailed" in result.stdout, result.stdout

    def test_an_infrastructure_error_still_surfaces(self, tmp_path: Path) -> None:
        """``raises=AssertionError`` is load-bearing and is NOT relaxed here.

        A crashed tier call raises something other than ``AssertionError``;
        it must red the run rather than be absorbed as "expected".
        """
        result = self._run(tmp_path, "raise RuntimeError('tier call crashed')")
        assert result.returncode != 0, (
            "a non-AssertionError must still fail the run, or the marker "
            f"would swallow an infrastructure error\n{result.stdout}"
        )
