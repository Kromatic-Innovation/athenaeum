# SPDX-License-Identifier: Apache-2.0
"""Offline proof of the DOMAIN TRUST OVERRIDES eval harness (issue
athenaeum#2332, TG4).

UNMARKED -- no ``pytest.mark.eval`` -- runs in the default suite, offline,
no credential, no network, against a scripted
:class:`unittest.mock.MagicMock` client (same shape as
``tests/evals/test_resolver_eval_stub.py``). Exercises the real
:func:`athenaeum.resolutions.propose_resolution` / ``_build_user_message``
through :mod:`tests.evals.trust_override_harness`'s `run_case` /
`aggregate`, not a re-implementation.

This proves the harness's win-selection-accuracy and edge-citation-rate
counting is correct, on a fixed 4-case set, via scripted correct/wrong
payloads -- it makes NO claim about a real model's behaviour. Setting a
pass bar from a live run is athenaeum#2365 (operator ruling, see
``docs/design/trust-graph.md`` §6), which this issue explicitly excludes
("the paid eval and its pass bar").
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from tests.evals.trust_override_harness import (
    TrustOverrideCase,
    aggregate,
    run_case,
)

_GOVDB_CONFIG = {
    "librarian": {
        "source_trust_enabled": True,
        "source_trust_edges": [
            {
                "domain": {"entity_type": "person", "field": "payout_amount"},
                "over": "api:govdb",
                "under": "user",
                "note": "a documentary government ledger beats self-report",
            }
        ],
    }
}

# Four hand-labeled pairs, all in the edge's domain -- the operator's own
# example from docs/design/trust-graph.md §1 ("a government DB beats the
# operator on payout figures"): member b (api:govdb) should win once the
# override is live, reversing the ordinary taxonomy's user-wins default.
_CASES = [
    TrustOverrideCase(
        case_id=f"payout_{i}",
        entity_type="person",
        field="payout_amount",
        a_source="user:conv-1",
        a_body=f"Payout is ${100000 + i * 1000}.",
        b_source="api:govdb",
        b_body=f"Payout is ${200000 + i * 1000}.",
        expected_winner="b",
    )
    for i in range(4)
]


def _client_returning(*payloads: str) -> MagicMock:
    """A client whose ``messages.create`` yields each payload in order,
    one per call -- enough for one case each, no retry."""
    client = MagicMock()
    responses = []
    for payload in payloads:
        response = MagicMock()
        response.content = [MagicMock(text=payload)]
        responses.append(response)
    client.messages.create.side_effect = responses
    return client


def _payload(*, winner: str, cites_edge: bool) -> str:
    precedence = (
        '["a:user:conv-1 ; b:api:govdb (trust edge override)"]'
        if cites_edge
        else '["a:user:conv-1 > b:api:govdb (ordinary precedence)"]'
    )
    action = "keep_b" if winner == "b" else "keep_a"
    return (
        '{"recommended_winner": "%s", "action": "%s", "confidence": 0.9, '
        '"rationale": "scored by the trust-override eval harness test", '
        '"source_precedence_used": %s}' % (winner, action, precedence)
    )


class TestHarnessCountsWinSelectionAccuracy:
    def test_all_correct_scores_1_0(self, tmp_path: Path) -> None:
        client = _client_returning(*[_payload(winner="b", cites_edge=True) for _ in _CASES])
        results = [
            run_case(case, client, tmp_path, config=_GOVDB_CONFIG) for case in _CASES
        ]
        agg = aggregate(results)
        assert agg.total == 4
        assert agg.win_selection_accuracy == 1.0

    def test_one_wrong_winner_drops_accuracy(self, tmp_path: Path) -> None:
        payloads = [_payload(winner="b", cites_edge=True) for _ in _CASES]
        payloads[0] = _payload(winner="a", cites_edge=True)  # one miss
        client = _client_returning(*payloads)
        results = [
            run_case(case, client, tmp_path, config=_GOVDB_CONFIG) for case in _CASES
        ]
        agg = aggregate(results)
        assert agg.win_selection_accuracy == 0.75


class TestHarnessCountsEdgeCitationF10:
    def test_block_rendered_and_cited_every_time(self, tmp_path: Path) -> None:
        client = _client_returning(*[_payload(winner="b", cites_edge=True) for _ in _CASES])
        results = [
            run_case(case, client, tmp_path, config=_GOVDB_CONFIG) for case in _CASES
        ]
        agg = aggregate(results)
        assert agg.blocks_rendered == 4
        assert agg.edge_citation_rate == 1.0
        assert agg.absent_citations == 0

    def test_absent_citation_counts_as_an_f10_miss_even_with_correct_winner(
        self, tmp_path: Path
    ) -> None:
        """F10: the resolver can pick the RIGHT winner while never citing
        the edge -- that must still count against edge_citation_rate, not
        get waved through because win-selection looked fine."""
        payloads = [_payload(winner="b", cites_edge=True) for _ in _CASES]
        payloads[1] = _payload(winner="b", cites_edge=False)  # right winner, no citation
        client = _client_returning(*payloads)
        results = [
            run_case(case, client, tmp_path, config=_GOVDB_CONFIG) for case in _CASES
        ]
        agg = aggregate(results)
        # Win-selection accuracy is untouched by the missing citation.
        assert agg.win_selection_accuracy == 1.0
        assert agg.blocks_rendered == 4
        assert agg.absent_citations == 1
        assert agg.edge_citation_rate == 0.75

    def test_overrides_off_renders_no_block_and_no_citation_rate(
        self, tmp_path: Path
    ) -> None:
        """With the flag off (AC1 path), no block renders at all -- the
        citation metric is undefined (None), not zero, since there was
        nothing to cite in the first place."""
        flag_off_config = {
            "librarian": {
                "source_trust_enabled": False,
                "source_trust_edges": _GOVDB_CONFIG["librarian"]["source_trust_edges"],
            }
        }
        client = _client_returning(*[_payload(winner="a", cites_edge=False) for _ in _CASES])
        results = [
            run_case(case, client, tmp_path, config=flag_off_config) for case in _CASES
        ]
        agg = aggregate(results)
        assert agg.blocks_rendered == 0
        assert agg.edge_citation_rate is None
        assert agg.absent_citations == 0
