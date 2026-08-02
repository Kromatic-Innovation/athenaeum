# SPDX-License-Identifier: Apache-2.0
"""Issue athenaeum#462 (slice B of athenaeum#460) — persist the C3 merge output BEFORE C4.

Until this change ``merge_clusters_to_wiki`` built every entry (C3,
deterministic), ran the deadline-checked C4 detector/resolver loop, and only
THEN wrote the pages. A C4 deadline trip — which happened on 10+ consecutive
nights (athenaeum#440) — raised before the write loop, so the ENTIRE C3 build was
discarded and every night re-paid C3 and banked nothing.

The fix writes the merged pages immediately after the C3 build (unflagged,
byte-identical to a deterministic ``client=None`` compile), runs C4, then
re-writes only the entries whose contradiction state changed. These tests lock
in the four properties that matter:

1. A C4 deadline trip leaves every C3 page on disk (the core regression).
2. The page is on disk (unflagged) at detection time (first-write precedes C4),
   and is re-written flagged once C4 detects (the re-write path).
3. A page flagged by a prior run whose cluster now clears is re-written
   unflagged (the athenaeum#145 flag-clear lifecycle survives the reorder).
4. Dry-run still writes nothing, even when detection would flag.

All Anthropic calls are stubbed; no live API, no network.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from athenaeum.contradictions import ContradictionResult
from athenaeum.merge import RunDeadlineExceeded, merge_clusters_to_wiki

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_SCOPE = "-Users-tristankromer-Code"


def _write_am(root: Path, name: str, body: str) -> None:
    d = root / "raw" / "auto-memory" / _SCOPE
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(
        f"---\nname: {name[:-3]}\ntype: auto-memory\n---\n{body}\n", encoding="utf-8"
    )


def _write_cluster(root: Path, rows: list[dict]) -> None:
    out = root / "raw" / "_librarian-clusters.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        "".join(json.dumps(r, sort_keys=True) + "\n" for r in rows), encoding="utf-8"
    )


def _seed_root(tmp_path: Path, n_clusters: int = 1) -> Path:
    """A knowledge root with ``n_clusters`` two-member clusters.

    Members carry no validity windows (both open → not disjoint) and declare
    no relationship, so the C4 detector actually RUNS on each cluster —
    exercising the real first-write / detect / re-write ordering rather than a
    short-circuit path. ``cross_scope_mode: off`` keeps C4 chunking to one
    chunk per entry so the deadline-trip test can count monotonic calls.
    """
    root = tmp_path / "knowledge"
    (root / "wiki").mkdir(parents=True)
    rows = []
    for i in range(n_clusters):
        a, b = f"feedback_a{i}.md", f"feedback_b{i}.md"
        _write_am(root, a, f"Cluster {i} says the price is $50 per month.")
        _write_am(root, b, f"Cluster {i} says the price is $70 per month.")
        rows.append(
            {
                "cluster_id": f"pricing-{i:04d}",
                "member_paths": [f"{_SCOPE}/{a}", f"{_SCOPE}/{b}"],
                "centroid_score": 0.62,
                "rationale": "cosine >= 0.55; shares tokens: price, per, month",
            }
        )
    _write_cluster(root, rows)
    (root / "athenaeum.yaml").write_text(
        "recall:\n  extra_intake_roots:\n    - raw/auto-memory\n"
        "contradiction:\n  cross_scope_mode: off\n",
        encoding="utf-8",
    )
    return root


class _StepClock:
    """A monotonic clock that advances a fixed delta on every call, so a test
    can place a deadline BETWEEN the last C3 boundary check and the first C4
    chunk check without sleeping. merge.py reads ``athenaeum.merge.time`` only
    at those two sites (the C3-row loop and the C4-chunk loop), so the call
    count is exactly ``n_rows`` C3 checks followed by the first C4 check."""

    def __init__(self, delta: float = 1000.0) -> None:
        self.t = 0.0
        self.delta = delta

    def monotonic(self) -> float:
        self.t += self.delta
        return self.t


def _flagged(page: Path) -> bool:
    return "status: contradiction-flagged" in page.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. The core regression: a C4 trip keeps the C3 pages.
# ---------------------------------------------------------------------------


def test_c4_deadline_trip_keeps_all_c3_pages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _seed_root(tmp_path, n_clusters=2)

    # Two cluster rows → two C3 boundary checks (calls 1,2 = 1000,2000), then
    # the first C4 chunk check (call 3 = 3000) trips. deadline=2500 sits in the
    # gap: C3 completes and both pages are first-written, then C4 raises.
    clock = _StepClock(delta=1000.0)
    monkeypatch.setattr("athenaeum.merge.time.monotonic", clock.monotonic)

    with pytest.raises(RunDeadlineExceeded) as excinfo:
        merge_clusters_to_wiki(root, client=None, deadline=2500.0)

    # Raised from the C4 loop, NOT the C3 loop — proving C3 finished first.
    assert excinfo.value.phase == "C4 contradiction detector / resolver"

    # Every C3 page is on disk despite the trip (the whole point of athenaeum#462).
    pages = sorted((root / "wiki").glob("auto-*.md"))
    assert len(pages) == 2, "both C3 pages must survive the C4 deadline trip"
    # And each is the clean deterministic C3 output — unflagged, since C4 never
    # completed a detection (the flag is only added after detection).
    for p in pages:
        assert not _flagged(p)


# ---------------------------------------------------------------------------
# 2. First-write precedes C4; C4 re-writes the flagged entry.
# ---------------------------------------------------------------------------


def test_first_write_precedes_detection_and_c4_rewrites_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _seed_root(tmp_path, n_clusters=1)
    seen: dict[str, object] = {}

    def _fake_detect(members, client, *, config=None, usage=None):
        # At detection time the page must already be on disk AND unflagged —
        # written by the pre-C4 first-write pass.
        pages = sorted((root / "wiki").glob("auto-*.md"))
        seen["page_present_at_detect"] = len(pages) == 1
        seen["unflagged_at_detect"] = bool(pages) and not _flagged(pages[0])
        return ContradictionResult(
            detected=True,
            conflict_type="factual",
            members_involved=[str(m.path) for m in members],
            conflicting_passages=["$50 per month", "$70 per month"],
            rationale="incompatible prices",
        )

    monkeypatch.setattr("athenaeum.merge.detect_contradictions", _fake_detect)

    entries = merge_clusters_to_wiki(root, client=None)

    assert seen["page_present_at_detect"] is True, "page must exist before C4"
    assert seen["unflagged_at_detect"] is True, "first write must be unflagged"
    # C4 detected → the page was re-written with the flag.
    assert len(entries) == 1
    assert entries[0].contradictions_detected is True
    page = next((root / "wiki").glob("auto-*.md"))
    assert _flagged(page), "C4 must re-write the page with the contradiction flag"


# ---------------------------------------------------------------------------
# 3. Flag-clear lifecycle survives the reorder (athenaeum#145).
# ---------------------------------------------------------------------------


def test_prior_flag_is_cleared_when_cluster_no_longer_conflicts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _seed_root(tmp_path, n_clusters=1)

    # Run 1: force detection → the page is written flagged.
    def _detect_true(members, client, *, config=None, usage=None):
        return ContradictionResult(
            detected=True,
            conflict_type="factual",
            members_involved=[str(m.path) for m in members],
            conflicting_passages=["$50 per month", "$70 per month"],
            rationale="incompatible prices",
        )

    monkeypatch.setattr("athenaeum.merge.detect_contradictions", _detect_true)
    merge_clusters_to_wiki(root, client=None)
    page = next((root / "wiki").glob("auto-*.md"))
    assert _flagged(page), "run 1 must leave the page flagged"

    # Run 2: the deterministic client=None path detects nothing → the prior
    # flag must be cleared by the unflagged first write (no stale flag left).
    monkeypatch.undo()
    entries = merge_clusters_to_wiki(root, client=None)
    assert entries[0].contradictions_detected is False
    assert not _flagged(page), "run 2 must clear the stale contradiction flag"
    assert "contradictions_detected: false" in page.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 4. Dry-run writes nothing, even when detection would flag.
# ---------------------------------------------------------------------------


def test_dry_run_writes_nothing_even_when_detection_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _seed_root(tmp_path, n_clusters=1)

    def _detect_true(members, client, *, config=None, usage=None):
        return ContradictionResult(
            detected=True,
            conflict_type="factual",
            members_involved=[str(m.path) for m in members],
            rationale="incompatible prices",
        )

    monkeypatch.setattr("athenaeum.merge.detect_contradictions", _detect_true)

    entries = merge_clusters_to_wiki(root, client=None, dry_run=True)

    # Detection still ran (entries carry the verdict), but NOTHING was written.
    assert entries and entries[0].contradictions_detected is True
    assert sorted((root / "wiki").glob("auto-*.md")) == []
    assert not (root / "wiki" / "_pending_questions.md").exists()


# ---------------------------------------------------------------------------
# 5. The reorder preserves out_wiki_root (athenaeum#359) and only_cluster_ids (athenaeum#370).
# ---------------------------------------------------------------------------


def test_out_wiki_root_redirect_still_honored_by_first_write(tmp_path: Path) -> None:
    """The pre-C4 first write must target ``out_wiki_root`` (the compile-as-of
    scratch dir), never the live wiki — otherwise the reorder would leak a
    recompiled snapshot into the live tree."""
    root = _seed_root(tmp_path, n_clusters=1)
    scratch = tmp_path / "scratch-wiki"
    entries = merge_clusters_to_wiki(root, client=None, out_wiki_root=scratch)
    assert len(entries) == 1
    # Written to the scratch dir...
    assert sorted(scratch.glob("auto-*.md")), "first write must honor out_wiki_root"
    # ...and NOT to the live wiki.
    assert sorted((root / "wiki").glob("auto-*.md")) == []


def test_only_cluster_ids_scopes_the_first_write(tmp_path: Path) -> None:
    """Delta scope (athenaeum#370) must still write ONLY the affected cluster's page —
    the first write iterates the already-delta-filtered ``entries``, so an
    unaffected cluster is never written (or rewritten)."""
    root = _seed_root(tmp_path, n_clusters=2)
    entries = merge_clusters_to_wiki(
        root, client=None, only_cluster_ids={"pricing-0000"}
    )
    assert len(entries) == 1
    assert entries[0].cluster_id == "pricing-0000"
    pages = sorted((root / "wiki").glob("auto-*.md"))
    assert len(pages) == 1, "delta scope must write exactly the one affected page"
