# SPDX-License-Identifier: Apache-2.0
"""Contracts the synthetic eval corpus generator must hold.

Offline and free -- these run in the default suite. They exist because the
corpus is an *instrument*: a measurement taken against a corpus that is
non-reproducible, internally inconsistent, or not actually competitive for
rank is not a weaker result, it is a meaningless one.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.evals.corpus import (
    SCALES,
    build_corpus,
    load_core_pages,
    load_probes,
    validate_core,
)


def test_core_corpus_is_internally_consistent() -> None:
    """Every probe's ground truth and every page link must resolve.

    A dangling ``expected_uids`` reference does not fail loudly at use time --
    it silently scores as a retrieval MISS, which reads as a model regression.
    A corpus error must never be able to masquerade as an eval result.
    """
    problems = validate_core(load_core_pages(), load_probes())
    assert not problems, "corpus inconsistencies:\n  " + "\n  ".join(problems)


def test_generation_is_deterministic_within_a_process() -> None:
    first = build_corpus(scale="small", seed=4242)
    second = build_corpus(scale="small", seed=4242)
    assert first.fingerprint() == second.fingerprint()


def test_generation_is_deterministic_ACROSS_processes() -> None:
    """The contract that actually matters, and the one a same-process test
    cannot see.

    Python salts string hashing per process, so any generation input derived
    from builtin ``hash()`` differs between runs while an in-process test
    passes -- which is precisely how this shipped broken once. A corpus that
    is not reproducible across processes makes every stored fingerprint name
    a corpus nobody can rebuild, so this must run in a SUBPROCESS.
    """
    script = (
        "from tests.evals.corpus import build_corpus; "
        "print(build_corpus(scale='small', seed=4242).fingerprint())"
    )
    env = {**os.environ, "PYTHONPATH": "src"}
    seen = {
        subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            check=True,
            cwd=Path(__file__).resolve().parent.parent,
            env=env,
        ).stdout.strip()
        for _ in range(3)
    }
    assert len(seen) == 1, f"fingerprint varies across processes: {seen}"


def test_different_seeds_yield_different_corpora() -> None:
    """Guards the inverse: a seed that is ignored would make every run identical
    and silently collapse replicates into one sample."""
    assert (
        build_corpus(scale="small", seed=1).fingerprint()
        != build_corpus(scale="small", seed=2).fingerprint()
    )


def test_core_scale_generates_nothing() -> None:
    corpus = build_corpus(scale="core")
    assert corpus.tier_counts() == {"core": len(load_core_pages())}


@pytest.mark.parametrize("scale", ["small", "medium", "medium_verydense"])
def test_scales_reach_their_page_floor(scale: str) -> None:
    """``total_pages`` is a floor ballast fills to, never a cap.

    Core and distractor pages are the measurement; trimming either to hit a
    page count would discard ground truth or retrieval pressure to satisfy
    padding. The floor must still be MET, or the size axis stops separating
    its points.
    """
    corpus = build_corpus(scale=scale)
    floor = SCALES[scale].total_pages
    assert len(corpus.pages) >= floor


def test_page_floors_leave_room_for_ballast() -> None:
    """Each size-axis point must actually produce ballast.

    A floor set below core+distractors silently yields zero ballast, and that
    scale stops being a distinct point on the size axis while still appearing
    in the grid as though it were one.
    """
    for scale in ("small", "medium", "large"):
        counts = build_corpus(scale=scale).tier_counts()
        assert counts.get("ballast", 0) > 0, f"{scale}: floor too low for ballast"


def test_size_and_confusability_are_independent_axes() -> None:
    """The design's load-bearing property.

    ``medium`` and ``medium_dense`` hold page count constant while varying
    near-miss density. If these two axes moved together, a recall drop could
    not be attributed to either, and the two causes have different fixes (a
    better index vs better disambiguation).
    """
    sparse = build_corpus(scale="medium")
    dense = build_corpus(scale="medium_dense")

    assert len(sparse.pages) == len(dense.pages), "size axis must not move"
    assert dense.tier_counts()["distractor"] > sparse.tier_counts()["distractor"] * 3, (
        "confusability axis must move"
    )


def test_distractors_actually_reach_the_top_k() -> None:
    """The confusability axis must move something. This is the load-bearing one.

    Vocabulary overlap is NOT the property that matters -- the first version of
    this suite asserted only that some distractor contained some probe term,
    which passed while distractors occupied 0 of 95 top-5 slots. The axis was
    inert and the test could not see it.

    What matters is rank competition: if near-misses never surface, raising
    their density changes nothing, and Workstream G's scale-dependence result
    -- read directly off this axis -- would be a measurement of noise.
    """
    import tempfile

    from athenaeum.mcp_server import recall_search
    from tests.evals.metrics import uids_from_recall_output

    corpus = build_corpus(scale="medium_verydense")
    root = Path(tempfile.mkdtemp())
    corpus.materialize(root)
    tier_of = {page.uid: page.tier for page in corpus.pages}

    slots = distractor_slots = 0
    for probe in corpus.probes:
        output = recall_search(root / "wiki", probe.query, top_k=5)
        hits = uids_from_recall_output(output)[:5]
        slots += len(hits)
        distractor_slots += sum(1 for uid in hits if tier_of.get(uid) == "distractor")

    assert slots, "no results at all -- the probe harness is broken, not the corpus"
    share = distractor_slots / slots
    assert share >= 0.10, (
        f"distractors took {distractor_slots}/{slots} top-5 slots "
        f"({share:.0%}); below ~10% the confusability axis cannot move a "
        "result and density is a knob attached to nothing"
    )


def test_distractors_share_probe_vocabulary() -> None:
    """Necessary-but-insufficient companion to the rank test above.

    Kept because it localizes a failure: if rank competition disappears, this
    says whether the cause was vocabulary (a template regression) or ranking.
    """
    corpus = build_corpus(scale="small")
    by_probe: dict[str, list[str]] = {}
    for page in corpus.pages:
        if page.tier == "distractor":
            by_probe.setdefault(page.uid.rsplit("-", 1)[0], []).append(
                (page.name + " " + page.body).lower()
            )

    for probe in corpus.probes:
        texts = by_probe.get(f"dis-{probe.id}", [])
        assert texts, f"probe {probe.id!r} generated no distractors"
        terms = [t.lower() for t in probe.distractor_terms] or [probe.query.lower()]
        assert any(any(term in text for term in terms) for text in texts), (
            f"probe {probe.id!r}: distractors share none of its vocabulary"
        )


def test_ballast_does_not_compete_with_probes() -> None:
    """The inverse guard: ballast must measure SIZE only.

    If ballast shared probe vocabulary it would be distractor mass under
    another name, and the two axes would be confounded from the start.
    """
    corpus = build_corpus(scale="medium")
    ballast = [p for p in corpus.pages if p.tier == "ballast"]
    assert ballast, "medium scale should produce ballast"

    distinctive = {
        term.lower() for probe in corpus.probes for term in probe.distractor_terms if len(term) > 6
    }
    for page in ballast[:400]:
        text = (page.name + " " + page.body).lower()
        overlap = {term for term in distinctive if term in text}
        assert not overlap, (
            f"ballast page {page.uid} shares probe vocabulary {overlap} -- "
            "that makes it a distractor and confounds the two axes"
        )


def test_probe_taxonomy_is_complete() -> None:
    """Every class must be populated, including the two that are usually missed.

    Abstention and disambiguation are the classes a retrieval suite most often
    lacks, and they are where a push sidecar does its real damage: confidently
    surfacing a wrong page beats surfacing nothing only if the page is right.
    """
    classes = {p.probe_class for p in load_probes()}
    assert {
        "single_hop",
        "multi_hop",
        "temporal",
        "disambiguation",
        "abstention",
        "distractor_robustness",
    } <= classes


def test_materialize_writes_a_readable_wiki_tree(tmp_path: Path) -> None:
    corpus = build_corpus(scale="core")
    wiki = corpus.materialize(tmp_path)
    written = list(wiki.glob("*.md"))
    assert len(written) == len(corpus.pages)

    sample = (wiki / "person-rowan-hale.md").read_text(encoding="utf-8")
    assert sample.startswith("---\n")
    assert "uid: person-rowan-hale" in sample
    assert "type: person" in sample
