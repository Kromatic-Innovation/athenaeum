# SPDX-License-Identifier: Apache-2.0
"""athenaeum#1682 AC3: ``run_cluster_comparator`` evaluates pairs one at a time,
with no chunk-level batching.

Deliberately a SEPARATE file from ``tests/test_cluster_comparator.py`` (a
concurrently-edited file in another lane) rather than an addition to it --
keeps this issue's footprint in that file at zero.

Why this test exists (issue athenaeum#1682's Motivation, item 3): C4's
``merge.py::_filter_declared_pairs`` needs an internal "partial prune" step
(``has_undeclared_partner`` bookkeeping) ONLY because C4 batches every
member of a cluster into ONE shared Haiku call, and the prune's job is to
drop chunk members that do not need to be in that shared call.
``cluster_comparator.run_cluster_comparator`` has no such batching to begin
with -- it calls :func:`athenaeum.comparator.compare_pages` once per
candidate pair (``cluster_comparator.py:258`` onward). With no chunk, there
is nothing for a "partial prune" to prune down to: the ported per-pair
declared-relationship check (athenaeum#1682 AC1/AC2, in ``compare_pages``
itself) already settles a declared pair before ITS OWN Gate 2 call, and an
undeclared pair in the same cluster is entirely unaffected -- there is no
shared batch for one pair's declaration to prune members out of. This test
pins that structural fact: for a cluster with N members, exactly
``C(N, 2)`` separate :func:`~athenaeum.comparator.compare_pages` calls are
made (never fewer via prune, never more via batching), each producing its
own independent outcome.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

from athenaeum.cluster_comparator import planned_pair_count, run_cluster_comparator
from athenaeum.comparator import ContentRelation
from athenaeum.models import AutoMemoryFile

_AUTO_ON: dict[str, object] = {"librarian": {"comparator_enabled": True}}


def _write_am(scope_dir: Path, filename: str, body: str) -> AutoMemoryFile:
    """Build a real-on-disk :class:`AutoMemoryFile` (mirrors
    ``tests/test_cluster_comparator.py``'s own ``_write_am`` helper -- kept
    local rather than imported so this file has no dependency on that
    concurrently-edited module)."""
    scope_dir.mkdir(parents=True, exist_ok=True)
    path = scope_dir / filename
    path.write_text(
        f"---\nname: {filename}\ntype: feedback\n---\n" + body + "\n",
        encoding="utf-8",
    )
    return AutoMemoryFile(
        path=path,
        origin_scope="scope-x",
        memory_type="feedback",
        name=filename,
    )


def _fake_client() -> MagicMock:
    """A MagicMock mirroring the Anthropic SDK's ``messages.create`` response
    shape, canned to resolve Gate 2 to ``compatible`` every time -- the
    outcome itself is irrelevant here; only the CALL COUNT matters."""
    payload = json.dumps(
        {
            "content_relation": ContentRelation.COMPATIBLE,
            "conflicting_passages": [],
            "predicate_a": "a-predicate",
            "predicate_b": "b-predicate",
            "rationale": "test rationale",
        }
    )
    client = MagicMock()
    response = MagicMock()
    response.content = [MagicMock(text=payload)]
    client.messages.create.return_value = response
    return client


def test_run_cluster_comparator_issues_one_gate2_call_per_pair_not_one_per_cluster(
    tmp_path: Path,
) -> None:
    """A 3-member cluster forms C(3, 2) = 3 candidate pairs. If the driver
    batched the whole cluster into a single Gate-2 call (the shape C4's
    ``_filter_declared_pairs``/``has_undeclared_partner`` prune exists to
    manage), the client would be called ONCE. It is instead called once
    PER PAIR -- proving no chunk exists for a partial-prune step to act on.
    """
    members = [
        _write_am(tmp_path, "member-a.md", "claim A"),
        _write_am(tmp_path, "member-b.md", "claim B"),
        _write_am(tmp_path, "member-c.md", "claim C"),
    ]
    assert planned_pair_count(members) == 3

    client = _fake_client()
    result = run_cluster_comparator(
        members, client, config=_AUTO_ON, cluster_id="cluster-1"
    )

    assert result.gate_enabled is True
    assert result.pair_count == 3
    # One compare_pages call landed per pair -- not one shared call for the
    # whole 3-member cluster, and not fewer than 3 via any prune step.
    assert client.messages.create.call_count == 3
    assert len(result.outcomes) == 3
    # Every pair is distinct -- confirms pairwise, not some collapsed/
    # deduplicated batch of fewer than 3 comparisons.
    pair_ids = {frozenset((a, b)) for a, b, _outcome in result.outcomes}
    assert len(pair_ids) == 3


def test_run_cluster_comparator_singleton_makes_no_calls_and_no_pairs(
    tmp_path: Path,
) -> None:
    """A single-member cluster forms zero pairs -- the driver must not
    invent a self-pair or batch the lone member into any call."""
    members = [_write_am(tmp_path, "member-a.md", "claim A")]
    assert planned_pair_count(members) == 0

    client = _fake_client()
    result = run_cluster_comparator(
        members, client, config=_AUTO_ON, cluster_id="cluster-singleton"
    )

    assert result.pair_count == 0
    assert result.outcomes == []
    client.messages.create.assert_not_called()
