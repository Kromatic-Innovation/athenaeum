# SPDX-License-Identifier: Apache-2.0
"""Tests for the cross-entity recurring-claim detector (issue #272).

Slice 1 of the #258 SSoT epic. The detector is READ-ONLY: it groups the
SAME claim restated across DIFFERENT wiki entities (different files / UIDs)
and emits a report. It mutates nothing under ``wiki/``.

A fake embedding provider is injected so the suite stays offline (no
chromadb / model). Restatements of one claim share a vector (cosine ~1.0);
genuinely distinct claims — even when they share vocabulary — get vectors
whose cosine falls below the threshold, exercising the false-positive guard.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from athenaeum.recurring_claims import (
    extract_claim_occurrences,
    find_recurring_claims,
    group_recurring_claims,
    render_report,
)

# ---------------------------------------------------------------------------
# Fake offline embedding provider
# ---------------------------------------------------------------------------

# Hand-assigned vectors per exact claim text. The three "venture" claims are
# restatements (near-identical vectors → cosine ~1.0). The two "Python"
# claims SHARE vocabulary but are distinct facts; their vectors' cosine is
# ~0.41, well under the 0.85 threshold → must NOT group (FP guard).
_VECTORS: dict[str, list[float]] = {
    "Kromatic is Tristan's primary venture": [1.0, 0.0, 0.0, 0.0],
    "Tristan's primary venture is Kromatic": [0.99, 0.1, 0.0, 0.0],
    "Kromatic is his main venture": [0.98, 0.0, 0.1, 0.0],
    "Tristan enjoys rock climbing on weekends": [0.0, 1.0, 0.0, 0.0],
    "Tristan writes Python code every single day": [0.0, 0.0, 1.0, 0.0],
    "Tristan avoids Python web frameworks entirely": [0.0, 0.0, 0.4, 0.9],
}


# All vectors are emitted at a fixed width: the first 4 dims carry the
# hand-assigned semantics above; the remaining ``_BUCKETS`` dims are a
# one-hot space reserved for UNKNOWN texts. A known text is zero across the
# bucket space; an unknown text is zero across the first 4 dims and carries a
# single 1.0 in a hash-chosen bucket. Result: two DISTINCT unknown claims are
# orthogonal (cosine 0, never group), and an unknown never collides with a
# known semantic vector — a genuine FP guard, not the old all-parallel
# ``[0,0,0,x]`` fallback whose vectors were pairwise cosine 1.0.
_BUCKETS = 4096
_DIM = 4 + _BUCKETS


def _fake_embed(texts: list[str]) -> list[list[float]]:
    out: list[list[float]] = []
    for t in texts:
        vec = [0.0] * _DIM
        known = _VECTORS.get(t.strip())
        if known is not None:
            vec[: len(known)] = list(known)
        else:
            h = int(hashlib.sha1(t.encode("utf-8")).hexdigest(), 16)
            vec[4 + (h % _BUCKETS)] = 1.0
        out.append(vec)
    return out


# ---------------------------------------------------------------------------
# Fixture: a wiki with one claim restated across 3 entities + distinct claims
# ---------------------------------------------------------------------------


def _write(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


@pytest.fixture
def wiki_root(tmp_path: Path) -> Path:
    root = tmp_path / "wiki"
    root.mkdir()

    # Three DISTINCT entities (different uids/files) each restate the SAME
    # venture claim as a footnote claim (slice C / #262 source claim).
    _write(
        root / "aaaa1111-tristan-profile.md",
        "---\n"
        "uid: aaaa1111\n"
        "type: auto-memory\n"
        "name: tristan-profile\n"
        "sources:\n"
        "  - session: s1\n"
        '    claim: "Kromatic is Tristan\'s primary venture"\n'
        "---\n"
        "Profile body.\n",
    )
    _write(
        root / "bbbb2222-tristan-career.md",
        "---\n"
        "uid: bbbb2222\n"
        "type: auto-memory\n"
        "name: tristan-career\n"
        "sources:\n"
        "  - session: s2\n"
        '    claim: "Tristan\'s primary venture is Kromatic"\n'
        "---\n"
        "Career body.\n",
    )
    _write(
        root / "cccc3333-tristan-wins.md",
        "---\n"
        "uid: cccc3333\n"
        "type: auto-memory\n"
        "name: tristan-quantified-wins\n"
        "sources:\n"
        "  - session: s3\n"
        '    claim: "Kromatic is his main venture"\n'
        "---\n"
        "Wins body.\n",
    )

    # Distinct claim via body-sentence fallback (no footnote claims).
    _write(
        root / "dddd4444-tristan-hobbies.md",
        "---\n"
        "uid: dddd4444\n"
        "type: entity\n"
        "name: tristan-hobbies\n"
        "---\n"
        "Tristan enjoys rock climbing on weekends.\n",
    )

    # Two claims that SHARE the word "Python" but are distinct facts, in two
    # different entities — the false-positive guard must keep them apart.
    _write(
        root / "eeee5555-coding-habits.md",
        "---\n"
        "uid: eeee5555\n"
        "type: auto-memory\n"
        "name: coding-habits\n"
        "sources:\n"
        "  - session: s5\n"
        '    claim: "Tristan writes Python code every single day"\n'
        "---\n"
        "Habits body.\n",
    )
    _write(
        root / "ffff6666-tooling-prefs.md",
        "---\n"
        "uid: ffff6666\n"
        "type: auto-memory\n"
        "name: tooling-prefs\n"
        "sources:\n"
        "  - session: s6\n"
        '    claim: "Tristan avoids Python web frameworks entirely"\n'
        "---\n"
        "Tooling body.\n",
    )

    # Underscore-prefixed metadata file must be ignored.
    _write(root / "_pending_questions.md", "not an entity\n")

    # MEMORY.md is the per-scope table-of-contents index, NOT a memory entity.
    # Its lines must never be mined as claims (see canonical wiki scan).
    _write(
        root / "MEMORY.md",
        "# Index\n\nKromatic is Tristan's primary venture\n",
    )

    return root


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


def test_extract_claim_occurrences_reads_footnote_and_sentence(wiki_root: Path):
    occ = extract_claim_occurrences(wiki_root)
    texts = {o.claim_text for o in occ}
    assert "Kromatic is Tristan's primary venture" in texts
    assert "Tristan enjoys rock climbing on weekends" in texts  # body fallback
    # underscore file excluded
    assert all(not o.entity_id.startswith("_") for o in occ)
    # footnote-claim entities use the footnote granularity
    profile = [o for o in occ if o.entity_id.startswith("aaaa1111")]
    assert profile and profile[0].granularity == "footnote"
    hobby = [o for o in occ if o.entity_id.startswith("dddd4444")]
    assert hobby and hobby[0].granularity == "sentence"


# ---------------------------------------------------------------------------
# Grouping — the acceptance criteria
# ---------------------------------------------------------------------------


def test_restatements_group_as_one_recurring_claim(wiki_root: Path):
    groups = find_recurring_claims(
        wiki_root, threshold=0.85, embedding_provider=_fake_embed
    )
    # Exactly one recurring claim: the venture restatement.
    assert len(groups) == 1
    g = groups[0]
    assert g.entity_count == 3
    member_texts = {o.claim_text for o in g.occurrences}
    assert member_texts == {
        "Kromatic is Tristan's primary venture",
        "Tristan's primary venture is Kromatic",
        "Kromatic is his main venture",
    }
    # All three appearances are in DISTINCT entities.
    assert len({o.entity_id for o in g.occurrences}) == 3


def test_distinct_claims_are_not_grouped(wiki_root: Path):
    groups = find_recurring_claims(
        wiki_root, threshold=0.85, embedding_provider=_fake_embed
    )
    grouped_texts = {o.claim_text for g in groups for o in g.occurrences}
    # The two Python claims share vocabulary but are distinct — never grouped.
    assert "Tristan writes Python code every single day" not in grouped_texts
    assert "Tristan avoids Python web frameworks entirely" not in grouped_texts
    assert "Tristan enjoys rock climbing on weekends" not in grouped_texts


def test_group_key_is_stable(wiki_root: Path):
    g1 = find_recurring_claims(
        wiki_root, threshold=0.85, embedding_provider=_fake_embed
    )
    g2 = find_recurring_claims(
        wiki_root, threshold=0.85, embedding_provider=_fake_embed
    )
    assert [g.key for g in g1] == [g.key for g in g2]
    assert g1[0].key  # non-empty


def test_same_entity_repeats_do_not_group(tmp_path: Path):
    """Two restatements within ONE entity are not a cross-entity recurrence."""
    root = tmp_path / "wiki"
    root.mkdir()
    _write(
        root / "aaaa1111-one.md",
        "---\n"
        "uid: aaaa1111\n"
        "type: auto-memory\n"
        "name: one\n"
        "sources:\n"
        "  - session: s1\n"
        '    claim: "Kromatic is Tristan\'s primary venture"\n'
        "  - session: s2\n"
        '    claim: "Tristan\'s primary venture is Kromatic"\n'
        "---\n"
        "Body.\n",
    )
    groups = find_recurring_claims(root, threshold=0.85, embedding_provider=_fake_embed)
    assert groups == []


def test_memory_index_file_is_excluded(wiki_root: Path):
    """MEMORY.md is a TOC index, not an entity — it must yield no occurrences."""
    occ = extract_claim_occurrences(wiki_root)
    assert all(o.entity_id != "MEMORY" for o in occ)
    # Its TOC line duplicates a venture claim verbatim; if MEMORY.md were
    # scanned the venture group would gain a 4th (bogus) occurrence.
    groups = find_recurring_claims(
        wiki_root, threshold=0.85, embedding_provider=_fake_embed
    )
    assert len(groups) == 1
    assert groups[0].entity_count == 3


def test_complete_linkage_does_not_chain_distinct_claims(tmp_path: Path):
    """A~B and B~C but A≪C must NOT fuse A and C (single-linkage bug guard).

    Vectors (padded to 4 dims): cos(A,B)=cos(B,C)=cos(25°)≈0.906 ≥ 0.85, but
    cos(A,C)=cos(50°)≈0.643 < 0.85. Each claim lives in a DISTINCT entity, so
    only the strictest pair may group — A and C may never share a group.
    """
    import math

    text_a = "alpha claim about the bridge"
    text_b = "beta claim near the middle"
    text_c = "gamma claim far from alpha"
    vecs = {
        text_a: [1.0, 0.0, 0.0, 0.0],
        text_b: [math.cos(math.radians(25)), math.sin(math.radians(25)), 0.0, 0.0],
        text_c: [math.cos(math.radians(50)), math.sin(math.radians(50)), 0.0, 0.0],
    }

    def _embed(texts: list[str]) -> list[list[float]]:
        return [list(vecs[t.strip()]) for t in texts]

    root = tmp_path / "wiki"
    root.mkdir()
    for uid, name, claim in (
        ("aaaa1111", "alpha", text_a),
        ("bbbb2222", "beta", text_b),
        ("cccc3333", "gamma", text_c),
    ):
        _write(
            root / f"{uid}-{name}.md",
            "---\n"
            f"uid: {uid}\n"
            "type: auto-memory\n"
            f"name: {name}\n"
            "sources:\n"
            "  - session: s1\n"
            f'    claim: "{claim}"\n'
            "---\n"
            "Body.\n",
        )

    groups = find_recurring_claims(root, threshold=0.85, embedding_provider=_embed)
    # No group may contain BOTH the A and C claims.
    for g in groups:
        member_texts = {o.claim_text for o in g.occurrences}
        assert not ({text_a, text_c} <= member_texts)
    # Exactly one group forms (the A–B pair); C stays ungrouped.
    assert len(groups) == 1
    assert {o.claim_text for o in groups[0].occurrences} == {text_a, text_b}


def test_sentence_fallback_handles_abbreviations_and_decimals(tmp_path: Path):
    """The body splitter must not break on ``U.S.`` or a decimal like ``3.5``."""
    root = tmp_path / "wiki"
    root.mkdir()
    _write(
        root / "aaaa1111-bio.md",
        "---\nuid: aaaa1111\ntype: entity\nname: bio\n---\n"
        "Tristan visited the U.S. Government office. He earned 3.5 stars.\n",
    )
    occ = extract_claim_occurrences(root)
    texts = {o.claim_text for o in occ}
    assert texts == {
        "Tristan visited the U.S. Government office",
        "He earned 3.5 stars",
    }
    assert all(o.granularity == "sentence" for o in occ)


def test_inactive_entities_are_skipped(wiki_root: Path):
    """A superseded entity must not contribute claim occurrences."""
    _write(
        wiki_root / "gggg7777-retired.md",
        "---\n"
        "uid: gggg7777\n"
        "type: auto-memory\n"
        "name: retired\n"
        "superseded_by: tristan-profile\n"
        "sources:\n"
        "  - session: s7\n"
        '    claim: "Kromatic is his main venture"\n'
        "---\n"
        "Retired body.\n",
    )
    occ = extract_claim_occurrences(wiki_root)
    assert all(not o.entity_id.startswith("gggg7777") for o in occ)


# ---------------------------------------------------------------------------
# Read-only guarantee
# ---------------------------------------------------------------------------


def test_no_wiki_files_are_mutated(wiki_root: Path):
    before = {
        p.name: (p.stat().st_mtime_ns, p.read_bytes())
        for p in wiki_root.iterdir()
        if p.is_file()
    }
    find_recurring_claims(wiki_root, threshold=0.85, embedding_provider=_fake_embed)
    after = {
        p.name: (p.stat().st_mtime_ns, p.read_bytes())
        for p in wiki_root.iterdir()
        if p.is_file()
    }
    assert before == after
    # No new files created under wiki/ either.
    assert set(before) == set(after)


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------


def test_render_report_is_valid_yaml_with_group(wiki_root: Path):
    import yaml

    groups = find_recurring_claims(
        wiki_root, threshold=0.85, embedding_provider=_fake_embed
    )
    occ = extract_claim_occurrences(wiki_root)
    entities = len({o.entity_id for o in occ})
    report = render_report(groups, threshold=0.85, entities_scanned=entities)
    parsed = yaml.safe_load(report)
    assert parsed["summary"]["recurring_claim_count"] == 1
    assert parsed["summary"]["entities_scanned"] == entities
    assert parsed["summary"]["threshold"] == 0.85
    rc = parsed["recurring_claims"][0]
    assert rc["entity_count"] == 3
    assert len(rc["occurrences"]) == 3
    assert rc["representative"]


# ---------------------------------------------------------------------------
# Degradation: no embeddings available
# ---------------------------------------------------------------------------


def test_no_embeddings_returns_empty(wiki_root: Path):
    groups = find_recurring_claims(
        wiki_root, threshold=0.85, embedding_provider=lambda texts: None
    )
    assert groups == []


def test_fewer_than_two_claims_short_circuits(tmp_path: Path):
    root = tmp_path / "wiki"
    root.mkdir()
    _write(
        root / "aaaa1111-solo.md",
        "---\nuid: aaaa1111\ntype: auto-memory\nname: solo\n"
        'sources:\n  - session: s1\n    claim: "only one claim here"\n---\nBody.\n',
    )

    def _boom(texts: list[str]):  # must not be called
        raise AssertionError("embedder should not be invoked for <2 claims")

    groups = group_recurring_claims(
        extract_claim_occurrences(root), threshold=0.85, embedding_provider=_boom
    )
    assert groups == []
