# SPDX-License-Identifier: Apache-2.0
"""Backfill-sidecar eval placeholder (issue #331; deferred until #328).

Per issue #331: "The backfill-sidecar golden set is EXPLICITLY deferred
until #328 lands — build the detector/resolver/recall sets + harness +
recording now; leave a clearly-marked stub directory for the sidecar set."

When #328 lands, replace this module with the parametrized backfill eval
following the shape of :mod:`test_detector_eval` / :mod:`test_resolver_eval`,
consuming ``tests/evals/data/backfill/cases.yaml``. Keep the
``pytest.mark.eval`` marker so it lands in the same deselected-by-default
suite.

Until then this module intentionally collects zero eval cases — but a
single skip node keeps the placeholder discoverable in the eval-run log
so a reader of the workflow output can see the deferred slice by name.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.eval


@pytest.mark.skip(reason="backfill-sidecar eval deferred until #328 lands")
def test_backfill_eval_deferred() -> None:
    pass
