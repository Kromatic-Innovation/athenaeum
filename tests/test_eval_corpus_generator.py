# SPDX-License-Identifier: Apache-2.0
"""Contracts the synthetic eval corpus generator must hold.

Offline and free -- these run in the default suite. They exist because the
corpus is an *instrument*: a measurement taken against a corpus that is
non-reproducible, internally inconsistent, or not actually competitive for
rank is not a weaker result, it is a meaningless one.
"""

from __future__ import annotations

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


def test_generation_is_deterministic() -> None:
    """Same seed and scale must yield a byte-identical tree.

    This is what lets large corpora be regenerated on demand instead of
    committed, and what makes ``(version, seed, scale)`` a sufficient citation
    for a stored measurement.
    """
    first = build_corpus(scale="small", seed=4242)
    second = build_corpus(scale="small", seed=4242)
    assert first.fingerprint() == second.fingerprint()


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


def test_distractors_share_probe_vocabulary() -> None:
    """Distractors must actually compete for rank.

    Generic filler would never be surfaced by BM25 for a probe query, so a
    corpus padded with it could reach any size with recall untouched -- and
    would license the false conclusion that scale is harmless. Every probe
    therefore needs near-misses carrying its own vocabulary.
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
