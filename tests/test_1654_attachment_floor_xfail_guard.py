# SPDX-License-Identifier: Apache-2.0
"""athenaeum#1654: the attachment eval's aggregate-floor test carries a
strict, `AssertionError`-scoped xfail marker so a red Live-API eval job (the
expected state while ``ATTACHMENT_FLOOR`` is unmet, per athenaeum#1580 AC3)
does not silently mask an unrelated regression.

This test is DELIBERATELY unmarked (no ``eval``/``live``/``embedding``) so it
runs in the default pytest selection, unlike
``tests/evals/test_attachment_eval.py`` itself (module-level
``pytestmark = pytest.mark.eval``, deselected by default). It never invokes
the eval — it only imports the module and inspects the marker object on
:func:`tests.evals.test_attachment_eval.test_attachment_aggregate_floor`, so
a future edit that weakens or removes the marker (e.g. dropping
``strict=True``, widening ``raises``, or deleting the marker outright) fails
THIS test instead of silently letting the job go green for the wrong reason.
"""

from __future__ import annotations

from tests.evals import test_attachment_eval as attachment_eval_module


def test_attachment_aggregate_floor_carries_strict_xfail() -> None:
    target = attachment_eval_module.test_attachment_aggregate_floor
    xfail_marks = [
        mark for mark in getattr(target, "pytestmark", []) if mark.name == "xfail"
    ]
    assert len(xfail_marks) == 1, (
        "test_attachment_aggregate_floor must carry exactly one "
        f"pytest.mark.xfail; found {len(xfail_marks)}"
    )
    mark = xfail_marks[0]
    assert mark.kwargs.get("strict") is True, (
        "the aggregate-floor xfail must be strict=True, or an unexpected "
        "pass (XPASS) would keep reporting green instead of failing the "
        "run the day the floor is actually met"
    )
    assert mark.kwargs.get("raises") is AssertionError, (
        "the aggregate-floor xfail must be scoped to raises=AssertionError, "
        "or an infrastructure error (e.g. a crashed tier call) would be "
        "swallowed as an 'expected' failure instead of surfacing"
    )
