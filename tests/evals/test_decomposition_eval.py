# SPDX-License-Identifier: Apache-2.0
"""Decomposition live-API eval — the one case a size gate cannot decide
(issue athenaeum#1581).

Three of this layer's four cases are settled by
:func:`athenaeum.tiers.check_page_size_gate`, which is a ``len()`` and a
heading regex. They need no key, they are graded in
``tests/test_eval_decomposition.py``, and they run in ordinary CI. Putting
them behind an ``eval`` marker would have hidden the layer's central finding
— that today every decomposition verdict is reached WITHOUT a model call —
behind a credential.

Case D is the exception, and it is why this layer records fixtures at all
(athenaeum#1581 AC5). The hub is already decomposed: ``Pelbridge Relay`` with
``Pelbridge Relay (costs)`` and ``Pelbridge Relay (rota)`` beside it, all
three already in the index. New intake arrives on the costs facet. Nothing
about page size is in question — the hub is small. What is in question is
whether the classify tier mints a NEW entity for a facet an existing
sub-page already covers, which would re-fragment a hub that was correctly
decomposed. That is a judgement about the world, so it is a real call
against the real classify model.

Deliberately overlapping the attachment eval's Case B, per the issue: the
two layers must agree that facet intake lands on the existing sub-page. A
disagreement between them is a finding, not a flake.

Scoring is over SHAPE — which entity names the classifier proposes as NEW —
never exact prose, matching every other layer's contract. One case, so the
"aggregate floor" other layers use degenerates to the case itself; the floor
is stated explicitly rather than left implicit.

Marker: ``pytest.mark.eval`` — deselected by default (see pyproject).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from athenaeum.models import RawFile, TokenUsage
from athenaeum.tiers import tier2_classify
from tests.evals.decomposition import Case, load_cases, materialize
from tests.evals.harness import (
    LAYER_DECOMPOSITION,
    RecordingClient,
    build_live_client,
    live_ready,
)

pytestmark = pytest.mark.eval

CASE_D = "decomposed_hub_new_intake"

#: N=1 metered case, so the floor IS the case. Stated rather than omitted:
#: the other layers' floors buy slack against single-case model noise across
#: a set, and a one-case set has no slack to buy. If this layer grows a
#: second metered case, give it a floor with the same one-case slack
#: ``CLASSIFY_FLOOR`` and ``MERGE_FLOOR`` reason from.
DECOMPOSITION_FLOOR = 1


def _case() -> Case:
    return next(case for case in load_cases() if case.id == CASE_D)


def _make_raw(case: Case) -> RawFile:
    return RawFile(
        path=Path(f"/tmp/eval-decomposition/sessions/{case.id}.md"),
        source="sessions",
        timestamp="20260301T120000Z",
        uuid8="dd110001",
        _content=case.intake_text,
    )


def _score(case: Case, entities: list[Any]) -> tuple[bool, str]:
    """Did the classifier decline to mint a new page for an existing facet?

    ``tier2_classify`` reports only entities it believes are NEW. The hub and
    both sub-pages are handed to it as ``matched_names``, so a correct run
    proposes nothing carrying the programme's name: the observation belongs
    on ``Pelbridge Relay (costs)``, which already exists.

    The failure this catches is specific and is the one the issue names —
    intake landing on "a new page" rather than the existing sub-page. A
    classifier that proposed ``Pelbridge Relay costs Q3`` would score well on
    any measure of "did it extract something" and would re-fragment a hub
    that was correctly decomposed.
    """
    names = [str(getattr(e, "name", "")) for e in entities]
    base = case.page.name.lower()
    offenders = [n for n in names if base.split()[0].lower() in n.lower()]
    if offenders:
        return False, f"minted new entities for an already-decomposed hub: {offenders}"
    return True, f"proposed no new page for the existing facet (names={names})"


@pytest.fixture(scope="module")
def _live_ready() -> None:
    ok, reason = live_ready()
    if not ok:
        pytest.skip(reason)


def test_decomposed_hub_keeps_facet_intake_off_a_new_page(
    tmp_path: Path,
    eval_record: bool,
    eval_session: Any,
    _live_ready: None,
) -> None:
    """Case D, against a real classify call. See the module docstring."""
    case = _case()
    wiki = materialize(case, tmp_path)
    matched = [case.page.name, *(sub.name for sub in case.subpages)]

    inner = build_live_client()
    client = RecordingClient(inner, record=eval_record, layer=LAYER_DECOMPOSITION)
    client.start_case(case.id)

    original_create = client.messages.create

    def _create(**params: Any) -> Any:
        response = original_create(**params)
        eval_session.observe_response(str(params.get("model", "")), response)
        return response

    client.messages.create = _create  # type: ignore[method-assign]

    usage = TokenUsage()
    entities = tier2_classify(
        _make_raw(case),
        matched,
        [case.page.type],
        list(case.page.tags),
        [case.page.access],
        client,
        wiki_root=wiki,
        usage=usage,
    )

    passed, detail = _score(case, entities)
    eval_session.record_case(
        LAYER_DECOMPOSITION,
        case.id,
        expected=case.expected_verdict,
        observed="no_new_page" if passed else "new_page_minted",
        passed=passed,
        detail=detail,
    )
    assert passed, detail


def test_decomposition_aggregate_floor(eval_session: Any) -> None:
    """One metered case, so the floor is the case. See :data:`DECOMPOSITION_FLOOR`."""
    passed, total = eval_session.layer_score(LAYER_DECOMPOSITION)
    if total == 0:
        pytest.skip("no decomposition cases ran (no live key)")
    assert passed >= DECOMPOSITION_FLOOR, f"decomposition layer scored {passed}/{total}"
