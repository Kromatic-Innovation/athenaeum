# SPDX-License-Identifier: Apache-2.0
"""Recorded-fixture replay tests (issue athenaeum#331 Layer 2).

Runs on every PR — zero network, zero cost — by replaying the recorded
live-API responses under ``tests/fixtures/recorded/`` through the same
parsers the live eval exercises. Same tests, real-shaped payloads.

The staleness contract (issue athenaeum#331): the replay client re-computes the
prompt hash the parser is about to send and compares it to the fixture's
stored hash. On mismatch it raises
:class:`tests.evals.harness.FixtureStaleError` with the exact
"fixture stale — re-run evals with --record" message documented in the
issue, so an operator sees the guidance directly in the failure.

**Empty-fixture policy (issue athenaeum#551).** Whether an empty layer directory
is tolerated is decided by the committed seeded-layers manifest at
``tests/fixtures/recorded/seeded-layers.yml``, not by the run:

- **never seeded** — a layer *not* listed in the manifest collects zero
  items and passes trivially. This is the state at PR-merge time before
  any ``evals.yml`` run has seeded fixtures, and passing is correct.
- **seeded, then lost** — a layer *listed* in the manifest whose directory
  is empty or missing is a silent coverage hole (audit finding H13). It
  fails the suite hard via
  :func:`test_seeded_manifest_layers_are_populated`, naming the layer and
  pointing at the record command, so a dropped fixture cannot pass
  unnoticed.

The manifest ships empty, so until athenaeum#610 seeds the first layer the behavior
is byte-identical to before: every layer is unlisted, every directory is
empty, and all replay tests pass trivially — zero-key ``develop`` CI stays
green. Once a layer is seeded, any edit to the module's prompt fails the
corresponding replay tests until fixtures are re-recorded. See
``tests/evals/README.md`` for the record command.
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
    LAYER_BACKFILL,
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

# ---------------------------------------------------------------------------
# Seeded-layers manifest (issue athenaeum#551) — the durable "has this layer ever been
# recorded" fact that an empty directory alone cannot express. A layer listed
# here must keep a non-empty fixtures directory; an unlisted layer is free to
# be empty (never-seeded). Ships empty, so behavior is unchanged until athenaeum#610.
# ---------------------------------------------------------------------------

SEEDED_MANIFEST_PATH = RECORDED_ROOT / "seeded-layers.yml"


def _seeded_layers() -> set[str]:
    """Layer names recorded as ever-seeded in the committed manifest.

    Missing file or ``seeded: {}`` both yield the empty set (never-seeded
    everywhere), which is the shipped state.
    """
    if not SEEDED_MANIFEST_PATH.is_file():
        return set()
    data = yaml.safe_load(SEEDED_MANIFEST_PATH.read_text(encoding="utf-8")) or {}
    seeded = data.get("seeded") or {}
    return set(seeded)


def _unpopulated_seeded_layers(
    seeded: set[str],
    present_ids: dict[str, list[str]],
) -> list[str]:
    """Seeded layers that have no fixtures on disk — the H13 coverage hole.

    Pure function of (manifest membership, on-disk fixture ids) so the guard
    logic is unit-testable without touching the real manifest or filesystem.
    A layer counts as populated iff ``present_ids`` maps it to a non-empty
    list; absent or empty means the seeded fixtures are gone.
    """
    return sorted(layer for layer in seeded if not present_ids.get(layer))


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
# Golden-set cases under adjudication (issue athenaeum#737)
# ---------------------------------------------------------------------------
#
# The first live recording (athenaeum#610, run 30760264305) surfaced two cases where
# the model's answer disagrees with the golden set's stored expectation. On
# inspection the model's answer is at least as defensible as the golden's in
# both, so neither side is being silently rewritten here: the cases are marked
# strict-xfail and adjudicated in athenaeum#737. `strict=True` means that if either
# side changes so the case starts passing, THIS test goes red and the mark has
# to be removed deliberately — an xfail that quietly starts passing is how a
# quarantine becomes permanent.
_DISPUTED: dict[str, str] = {
    # athenaeum#737 adjudicated `tool_choice_editor`: the golden was corrected
    # to conflict_type 'factual' (the model's answer), so it is no longer
    # disputed and its detector case now passes without an xfail.
    # athenaeum#760/athenaeum#715: this resolver-side xfail STAYS. Removing it
    # needs a re-record (the recorded response is `scope_a`, which
    # `_classify_proposal` maps to `scope_a` != golden `keep_pick_winner`), and
    # a re-record needs a live backend this offline issue does not use. The
    # golden's rationale is recorded on the case in
    # tests/evals/data/resolver/cases.yaml; the xfail is removed when athenaeum#715
    # removes the resolver. This mark is TRACKED, not an untracked failure.
    "decision_conflict_hosting_migration": (
        "athenaeum#737: golden expects action_class 'keep_pick_winner'; the model "
        "returns 'scope_a'. The members are a Feb Heroku decision superseded by "
        "a May Fly.io cutover — temporal supersession, not a live contradiction "
        "with a winner to pick. Kept per athenaeum#760; removed by athenaeum#715 "
        "(needs a re-record)."
    ),
}


def _params(ids: list[str]) -> list[Any]:
    """Parametrize *ids*, strict-xfailing the cases under adjudication."""
    return [
        pytest.param(i, marks=pytest.mark.xfail(strict=True, reason=_DISPUTED[i]))
        if i in _DISPUTED
        else i
        for i in ids
    ]


# ---------------------------------------------------------------------------
# Detector replay
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _DETECTOR_IDS, reason=_EMPTY_LAYER_REASON)
@pytest.mark.parametrize("case_id", _params(_DETECTOR_IDS) or ["_placeholder_"])
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
@pytest.mark.parametrize("case_id", _params(_RESOLVER_IDS) or ["_placeholder_"])
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
    # Issue athenaeum#980 AC4: recall_search's push-metrics instrumentation now
    # writes behind the seam (wiki_root=), and wiki_root here is the REAL,
    # tracked ``tests/evals/data/recall/wiki/`` fixture, not a tmp copy — this
    # test asserts the replay pipeline runs end-to-end, not push-metrics
    # recording, so disable instrumentation rather than let a push record
    # land in source-controlled fixture data on every run.
    monkeypatch.setenv("ATHENAEUM_PUSH_METRICS_ENABLED", "0")
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


# ---------------------------------------------------------------------------
# Seeded-layers guard (issue athenaeum#551) — the H13 finding. Runs on every PR with
# zero network. With the shipped empty manifest it passes trivially; it turns
# a *seeded-then-lost* layer into a hard failure so a dropped fixture cannot
# silently downgrade to "empty, passes trivially".
# ---------------------------------------------------------------------------

# Every layer that could hold recorded fixtures, so the guard can look up the
# on-disk state of any name the manifest might list.
_ALL_KNOWN_LAYERS = (LAYER_DETECTOR, LAYER_RESOLVER, LAYER_RECALL, LAYER_BACKFILL)


def test_seeded_manifest_layers_are_populated() -> None:
    """A layer listed in seeded-layers.yml must have fixtures on disk.

    This is the H13 guard: an empty directory for a *never-seeded* layer is
    fine (see the empty-fixture policy in the module docstring), but an empty
    directory for a layer the manifest says was seeded means the fixtures were
    lost — a silent coverage hole. Ships green because the manifest is empty.
    """
    seeded = _seeded_layers()
    present_ids = {layer: _recorded_case_ids(layer) for layer in _ALL_KNOWN_LAYERS}
    # Cover any manifest layer name that isn't one of the four known layers.
    for layer in seeded:
        present_ids.setdefault(layer, _recorded_case_ids(layer))

    missing = _unpopulated_seeded_layers(seeded, present_ids)
    assert not missing, (
        "seeded-layers.yml lists layer(s) with no recorded fixtures on disk: "
        f"{missing}. Their fixtures were lost (a silent coverage hole), or the "
        "manifest entry is premature. Restore the fixtures, re-record with "
        "evals.yml (record=true) — see tests/evals/README.md — or remove the "
        f"stale manifest entry. Manifest: {SEEDED_MANIFEST_PATH}"
    )


def test_guard_fires_for_seeded_but_empty_layer() -> None:
    """A manifest-listed layer whose directory is empty is reported missing."""
    present = {"detector": [], "resolver": ["resolver-case-1"]}
    # detector is seeded but empty -> flagged; resolver is seeded and populated.
    assert _unpopulated_seeded_layers({"detector", "resolver"}, present) == [
        "detector"
    ]
    # A seeded layer entirely absent from the on-disk lookup is also flagged.
    assert _unpopulated_seeded_layers({"recall"}, {}) == ["recall"]


def test_guard_passes_for_unlisted_empty_layer() -> None:
    """An empty layer that is NOT in the manifest never fails the guard."""
    present = {"detector": [], "resolver": []}
    # Nothing seeded -> nothing required, even though every directory is empty.
    assert _unpopulated_seeded_layers(set(), present) == []
    # A seeded-and-populated layer is fine alongside unlisted empty layers.
    assert _unpopulated_seeded_layers({"resolver"}, {"resolver": ["c1"]}) == []


def test_manifest_records_the_610_seeding() -> None:
    """The manifest names the layers athenaeum#610 seeded (it shipped empty under athenaeum#551).

    This replaces `test_shipped_manifest_is_empty`, whose whole purpose was to
    pin the pre-seeding state until athenaeum#610 ran. Now that it has, the assertion
    that carries weight is the opposite one: every layer recorded by run
    30760264305 must stay listed, so that losing a layer's fixtures trips
    `test_seeded_manifest_layers_are_populated` instead of passing trivially.
    """
    assert _seeded_layers() == {
        "classify",
        "detector",
        "merge",
        "recall",
        "resolver",
    }
