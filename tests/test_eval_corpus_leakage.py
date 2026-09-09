# SPDX-License-Identifier: Apache-2.0
"""Leakage guard for the SYNTHETIC EVAL CORPUS (``tests/evals/data/corpus``).

Distinct from ``test_corpus_pii_lint.py``, which gates the *live* knowledge
corpus for inline contact data (athenaeum#495). This module gates the
hand-authored and generated eval fixtures for content borrowed from a real
knowledge tree. Same word, two corpora -- see ``tests/evals/data/corpus/README.md``.

Runs in the DEFAULT suite -- offline, no network, no API key -- so a fixture
that quotes real content fails an ordinary PR rather than waiting for a
metered eval run. athenaeum is a public repository; every fixture here is
public or public-bound.

The denylist of real proper nouns is read from the local knowledge tree AT
LINT TIME and never written to disk. A committed denylist of real names would
itself be the leak it exists to prevent -- the same "safety artifact living in
the same namespace as the thing it certifies" failure mode this workspace has
been bitten by before.

With no local knowledge tree present the denylist check SKIPS LOUDLY rather
than passing silently, so an outside contributor's suite still runs while a
maintainer's still checks. A silent pass would be worse than no check: it
would report safety it never established.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
import yaml

from athenaeum.pii import scan_corpus_pii
from tests.evals.corpus import CORPUS_ROOT, build_corpus

# What counts as an identity worth guarding.
#
# A real knowledge tree titles pages with ordinary English nouns ("retention",
# "onboarding", "pricing") as readily as with real names. Those titles are not
# identities, and treating them as leaks makes the check fire constantly on
# nothing -- and a noisy guard is a guard somebody switches off. The first run
# of this test failed on exactly that: the word "retention".
#
# So the rule is about SHAPE, not a growing list of exceptions:
#   * multi-word names are always checked -- "Rowan Hale", "Hale Associates";
#     that is where people and orgs live
#   * single tokens are checked unless they are common English words, since a
#     lowercase single-token identity (a repo or product name) is precisely
#     the case this corpus exists to test
_MIN_NAME_LEN = 5

#: Common English words that appear as real page titles but identify nobody.
#: Not an exception list to append to per failure -- if a new false positive
#: is an ordinary English word it belongs here; if it is not, it is a leak.
_COMMON_WORDS = frozenset(
    """
    about above after agent agenda alias analysis annual answer approach
    archive article assets audience audit backlog backup billing branch budget
    build cache calendar capacity change channel charter checklist claims
    client cohort compare config context contract cutover daily dashboard
    decision default delivery deploy design digest domain draft during effort
    email engine entity every example expense export feature filter finance
    first format forward funnel future github google growth handbook hiring
    holiday impact income index insight intake invoice issue journey launch
    layout leave ledger legal lifecycle margin market meeting memory metric
    migration mission model month notes notion object offsite onboarding
    output overview owner partner payment payroll pipeline planning playbook
    pricing principle priority process product profile program project
    proposal python quality quarter queue rating recall record refund release
    remote report request research retention retro revenue review roadmap
    router runbook salary sample schedule scope search security service
    session sheets since skill source sprint staffing standup status storage
    strategy stream summary support survey system target taxonomy template
    testing their there these thing third those threshold timeline tooling
    traffic training transfer under until update upgrade usage vendor version
    vision weekly where which while whose workflow would
    """.split()
)


def _knowledge_wiki() -> Path | None:
    root = Path(os.environ.get("ATHENAEUM_KNOWLEDGE_ROOT", Path.home() / "knowledge"))
    wiki = root / "wiki"
    return wiki if wiki.is_dir() else None


def _real_proper_nouns(wiki: Path, *, limit: int = 4000) -> set[str]:
    """Collect ``name``/``aliases`` values from real wiki frontmatter.

    Frontmatter only, deliberately -- names are the leak that matters and the
    one extractable precisely. Scanning bodies would return mostly prose and
    drown the signal in false positives.
    """
    names: set[str] = set()
    for path in sorted(wiki.rglob("*.md"))[:limit]:
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if not text.startswith("---"):
            continue
        end = text.find("\n---", 3)
        if end == -1:
            continue
        try:
            front = yaml.safe_load(text[3:end]) or {}
        except yaml.YAMLError:
            continue
        if not isinstance(front, dict):
            continue
        candidates = [front.get("name")]
        aliases = front.get("aliases")
        if isinstance(aliases, list):
            candidates.extend(aliases)
        for value in candidates:
            if not isinstance(value, str):
                continue
            cleaned = value.strip().lower()
            if len(cleaned) < _MIN_NAME_LEN:
                continue
            # Single common English words are page titles, not identities.
            # Multi-word names always qualify.
            if " " not in cleaned and cleaned in _COMMON_WORDS:
                continue
            names.add(cleaned)
    return names


def _corpus_blobs() -> dict[str, str]:
    """Hand-authored sources plus a generated sample, keyed by label.

    Generated tiers are checked too: composed names can collide with a real
    name by chance, which is exactly why composition alone is not trusted.
    """
    blobs = {
        str(path.relative_to(CORPUS_ROOT)): path.read_text(encoding="utf-8")
        for path in sorted(CORPUS_ROOT.rglob("*.yaml"))
    }
    generated = build_corpus(scale="small")
    blobs["<generated:small>"] = "\n".join(
        page.to_markdown() for page in generated.pages if page.tier != "core"
    )
    return blobs


def test_eval_corpus_contains_no_real_proper_nouns() -> None:
    wiki = _knowledge_wiki()
    if wiki is None:
        pytest.skip(
            "no local knowledge tree -- denylist check cannot run here "
            "(skipping loudly rather than passing silently)"
        )
    denylist = _real_proper_nouns(wiki)
    if not denylist:
        pytest.skip("local knowledge tree yielded no frontmatter names to check")

    hits: list[str] = []
    for label, blob in _corpus_blobs().items():
        lowered = blob.lower()
        for name in denylist:
            if re.search(rf"\b{re.escape(name)}\b", lowered):
                hits.append(f"{label}: {name!r}")
    assert not hits, (
        "eval corpus contains proper nouns that also appear in the local "
        "knowledge tree -- rewrite them as invented names (this corpus is "
        "public-bound):\n  " + "\n  ".join(sorted(hits)[:25])
    )


def test_denylist_shape_rule_admits_identities_and_rejects_common_words(
    tmp_path: Path,
) -> None:
    """Pin the shape rule itself, so relaxing it cannot go unnoticed.

    The rule is the whole guard: too strict and it fires on every ordinary
    page title until someone disables it; too loose and a real single-token
    identity (a repo or product name) walks straight through.
    """
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    for slug, name in [
        ("a", "Retention"),  # common word -> not an identity
        ("b", "Marisol Quenwick"),  # multi-word -> identity
        ("c", "zenodotus"),  # single lowercase token, not common -> identity
    ]:
        (wiki / f"{slug}.md").write_text(
            f"---\nuid: {slug}\nname: {name}\n---\n\nbody\n", encoding="utf-8"
        )

    found = _real_proper_nouns(wiki)
    assert "retention" not in found
    assert "marisol quenwick" in found
    assert "zenodotus" in found


def test_eval_corpus_has_no_local_paths() -> None:
    """No home directories or absolute local paths in a public-bound fixture.

    Unconditional -- unlike the denylist check this needs no local knowledge
    tree, so it protects outside contributors' PRs too.
    """
    pattern = re.compile(r"(/Users/|/home/|~/[A-Za-z])")
    hits = [label for label, blob in _corpus_blobs().items() if pattern.search(blob)]
    assert not hits, f"absolute or home-relative local paths in eval corpus: {hits}"


def test_materialized_eval_corpus_carries_no_contact_data(tmp_path: Path) -> None:
    """Reuse the production scanner rather than re-implementing one.

    ``scan_corpus_pii`` (athenaeum#495) already finds email/phone-shaped tokens
    across a wiki tree. Running the real scanner over the materialized eval
    corpus also keeps the fixtures honest as that scanner gets stricter.
    """
    wiki_root = build_corpus(scale="small").materialize(tmp_path)
    findings = scan_corpus_pii(wiki_root)
    assert not findings, (
        "synthetic eval corpus carries contact-shaped tokens: "
        f"{[str(f.path) for f in findings][:10]}"
    )
