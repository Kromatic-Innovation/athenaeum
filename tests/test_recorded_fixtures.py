# SPDX-License-Identifier: Apache-2.0
"""Recorded-fixture replay tests (issue #331 Layer 2).

Runs on every PR — zero network, zero cost — by replaying the recorded
live-API responses under ``tests/fixtures/recorded/`` through the same
parsers the live eval exercises. Same tests, real-shaped payloads.

The staleness contract (issue #331): the replay client re-computes the
prompt hash the parser is about to send and compares it to the fixture's
stored hash. On mismatch it raises
:class:`tests.evals.harness.FixtureStaleError` with the exact
"fixture stale — re-run evals with --record" message documented in the
issue, so an operator sees the guidance directly in the failure.

**Empty-fixture policy.** When a layer directory has zero recorded
fixtures (the state at PR-merge time before any eval-workflow run has
seeded them), the parametrized test collects zero items and passes
trivially. Real coverage begins the first time ``evals.yml`` is
dispatched with ``record=true``; from then on any edit to the module's
prompt fails the corresponding replay tests until fixtures are
re-recorded. See ``tests/evals/README.md`` for the record command.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from athenaeum.contradictions import ContradictionResult, detect_contradictions
from athenaeum.mcp_server import recall_search
from athenaeum.models import AutoMemoryFile
from athenaeum.query_topics import extract_topics
from athenaeum.resolutions import (
    MergeProposal,
    ResolutionProposal,
    propose_resolution,
)
from tests.evals.harness import (
    EVAL_DATA_ROOT,
    LAYER_DETECTOR,
    LAYER_RECALL,
    LAYER_RESOLVER,
    RECORDED_ROOT,
    FixtureStaleError,
    RecordedResponse,
    prompt_hash,
    replay_client,
    save_recorded,
)

# ---------------------------------------------------------------------------
# Fixture discovery — collect only what's on disk so an empty layer no-ops.
# ---------------------------------------------------------------------------


def _recorded_case_ids(layer: str) -> list[str]:
    layer_dir = RECORDED_ROOT / layer
    if not layer_dir.is_dir():
        return []
    return sorted(p.stem for p in layer_dir.glob("*.json"))


# Discovered once at collection time — used by ``@pytest.mark.parametrize``
# below and by the skip guard when a layer is empty (state at PR-merge time
# until the first evals.yml run seeds fixtures).
_DETECTOR_IDS = _recorded_case_ids(LAYER_DETECTOR)
_RESOLVER_IDS = _recorded_case_ids(LAYER_RESOLVER)
_RECALL_IDS = _recorded_case_ids(LAYER_RECALL)

_EMPTY_LAYER_REASON = (
    "no recorded fixtures — run evals.yml with record=true (or "
    "pytest -m eval --record locally) to seed"
)


def _load_golden(layer: str) -> dict[str, dict[str, Any]]:
    cases_path = EVAL_DATA_ROOT / layer / "cases.yaml"
    if not cases_path.is_file():
        return {}
    entries = yaml.safe_load(cases_path.read_text(encoding="utf-8")) or []
    return {str(entry["id"]): dict(entry) for entry in entries}


# ---------------------------------------------------------------------------
# Shared materialisation helpers (mirror the eval-suite helpers so the
# on-disk prompt matches byte-for-byte — the prompt-hash staleness contract
# depends on it).
# ---------------------------------------------------------------------------


def _materialise_members(
    scope_dir: Path,
    case: dict[str, Any],
) -> list[AutoMemoryFile]:
    scope_dir.mkdir(parents=True, exist_ok=True)
    members: list[AutoMemoryFile] = []
    for spec in case["members"]:
        fm = spec.get("frontmatter") or {}
        fm_lines = ["---"]
        for key, value in fm.items():
            fm_lines.append(f"{key}: {value}")
        fm_lines.append("---")
        body = str(spec["body"]).rstrip()
        path = scope_dir / spec["filename"]
        path.write_text("\n".join(fm_lines) + "\n" + body + "\n", encoding="utf-8")
        members.append(
            AutoMemoryFile(
                path=path,
                origin_scope=scope_dir.name,
                memory_type=str(fm.get("type", "feedback")),
                name=str(fm.get("name", spec["filename"])),
                source_type=str(fm.get("source_type", "inferred")),
                source_ref=str(fm.get("source_ref", "")),
                valid_from=str(fm.get("valid_from", "")),
                valid_until=str(fm.get("valid_until", "")),
            )
        )
    return members


# ---------------------------------------------------------------------------
# Detector replay
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _DETECTOR_IDS, reason=_EMPTY_LAYER_REASON)
@pytest.mark.parametrize("case_id", _DETECTOR_IDS or ["_placeholder_"])
def test_detector_replay(case_id: str, tmp_path: Path) -> None:
    golden = _load_golden(LAYER_DETECTOR)
    assert case_id in golden, (
        f"recorded fixture {case_id!r} has no matching golden-set case "
        f"in {LAYER_DETECTOR}/cases.yaml — delete the stray fixture or "
        "add the case."
    )
    case = golden[case_id]
    scope_dir = tmp_path / f"scope-{case_id}"
    members = _materialise_members(scope_dir, case)

    # replay_client enforces the staleness contract on messages.create —
    # any drift in the current prompt from the fixture's stored hash
    # fails here with the "re-run evals with --record" message.
    client = replay_client(LAYER_DETECTOR, case_id)
    result = detect_contradictions(members, client)

    expected = case["expected"]
    assert bool(result.detected) is bool(expected.get("detected"))
    if result.detected and expected.get("conflict_type") is not None:
        assert result.conflict_type == expected["conflict_type"]


# ---------------------------------------------------------------------------
# Resolver replay
# ---------------------------------------------------------------------------


def _classify_proposal(proposal: Any) -> str:
    if isinstance(proposal, MergeProposal):
        return "propose_merge"
    if not isinstance(proposal, ResolutionProposal):
        return "unknown"
    if proposal.action == "not_a_conflict":
        return "not_a_conflict"
    if (
        proposal.action == "retain_both_with_context"
        and proposal.disambiguation_options
    ):
        return "disambiguation"
    if proposal.action in ("keep_a", "keep_b", "correct_a", "correct_b"):
        return "keep_pick_winner"
    return proposal.action


def _detector_result(case: dict[str, Any], members: list[AutoMemoryFile]) -> ContradictionResult:
    det = case["detector"]
    return ContradictionResult(
        detected=True,
        conflict_type=det.get("conflict_type"),
        members_involved=[f"{m.origin_scope}/{m.path.name}" for m in members[:2]],
        conflicting_passages=list(det.get("passages") or []),
        rationale=str(det.get("rationale", "")),
    )


@pytest.mark.skipif(not _RESOLVER_IDS, reason=_EMPTY_LAYER_REASON)
@pytest.mark.parametrize("case_id", _RESOLVER_IDS or ["_placeholder_"])
def test_resolver_replay(case_id: str, tmp_path: Path) -> None:
    golden = _load_golden(LAYER_RESOLVER)
    assert case_id in golden, (
        f"recorded fixture {case_id!r} has no matching golden-set case "
        f"in {LAYER_RESOLVER}/cases.yaml — delete the stray fixture or "
        "add the case."
    )
    case = golden[case_id]
    scope_dir = tmp_path / f"scope-{case_id}"
    members = _materialise_members(scope_dir, case)
    detector = _detector_result(case, members)

    client = replay_client(LAYER_RESOLVER, case_id)
    proposal = propose_resolution(detector, members, client)

    observed = _classify_proposal(proposal)
    assert observed == case["expected"]["action_class"], (
        f"resolver replay {case_id}: expected "
        f"{case['expected']['action_class']!r}, got {observed!r}"
    )


# ---------------------------------------------------------------------------
# Recall replay — same monkeypatch pattern as the eval, but with the
# replay-stub client so no network fires.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _RECALL_IDS, reason=_EMPTY_LAYER_REASON)
@pytest.mark.parametrize("case_id", _RECALL_IDS or ["_placeholder_"])
def test_recall_replay(
    case_id: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    golden = _load_golden(LAYER_RECALL)
    assert case_id in golden, (
        f"recorded fixture {case_id!r} has no matching golden-set case "
        f"in {LAYER_RECALL}/cases.yaml — delete the stray fixture or "
        "add the case."
    )
    case = golden[case_id]

    stub = replay_client(LAYER_RECALL, case_id)
    import anthropic

    monkeypatch.setattr(anthropic, "Anthropic", lambda **kw: stub)
    # ``extract_topics`` short-circuits when ``ANTHROPIC_API_KEY`` is
    # unset — force-set a dummy value so the replay path executes without
    # requiring the CI environment to plumb a real secret. The stub
    # client short-circuits the network anyway.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fixture-replay-no-network")
    topics = extract_topics(case["prompt"], timeout=15.0)

    query = " ".join(topics) if topics else case["prompt"]
    wiki_root = EVAL_DATA_ROOT / "recall" / "wiki"
    output = recall_search(
        wiki_root,
        query,
        top_k=6,
        search_backend="keyword",
        cache_dir=tmp_path / "cache",
    )

    # Detector-replay-style assertion: replay confirms the parser accepts
    # the real-shaped response body. The eval suite is what asserts the
    # aggregate quality of the topic list; here we only assert the pipeline
    # runs end-to-end — a stale fixture would already have raised
    # FixtureStaleError before we got this far.
    assert isinstance(output, str)
    assert output  # non-empty


# ---------------------------------------------------------------------------
# Staleness contract self-test — runs on every PR so the contract itself
# stays green regardless of whether any recorded fixtures have been seeded.
# ---------------------------------------------------------------------------


def test_staleness_contract(tmp_path: Path) -> None:
    """Prove the replay client raises FixtureStaleError on a hash mismatch.

    Writes a synthetic fixture whose stored prompt-hash was generated
    against ``system="sys-A"``, then invokes the replay stub with
    ``system="sys-B"``. The stub must raise :class:`FixtureStaleError`
    and it must NOT be swallowed by any ``except Exception`` guard on
    the call path (see the FixtureStaleError docstring for the
    BaseException rationale).
    """
    # Use a case_id that cannot collide with real fixtures.
    case_id = "_staleness_contract_probe_"
    layer = LAYER_DETECTOR
    original_hash = prompt_hash(
        "test-model", "sys-A", [{"role": "user", "content": "hello"}]
    )
    rec = RecordedResponse(
        case_id=case_id,
        layer=layer,
        model="test-model",
        prompt_hash=original_hash,
        response_text="{}",
        usage={
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        },
        recorded_at="2026-07-08T00:00:00Z",
    )
    save_recorded(rec)
    try:
        stub = replay_client(layer, case_id)
        # Matching call passes cleanly.
        stub.messages.create(
            model="test-model",
            system="sys-A",
            messages=[{"role": "user", "content": "hello"}],
        )
        # Drifted call raises loudly — FixtureStaleError inherits from
        # BaseException so ``except Exception`` cannot swallow it.
        with pytest.raises(FixtureStaleError, match="fixture stale"):
            stub.messages.create(
                model="test-model",
                system="sys-B",  # drift
                messages=[{"role": "user", "content": "hello"}],
            )
    finally:
        # Never leave the probe fixture in the working tree — it would show
        # up as an untracked stray in the record-mode artifact upload.
        (RECORDED_ROOT / layer / f"{case_id}.json").unlink(missing_ok=True)
