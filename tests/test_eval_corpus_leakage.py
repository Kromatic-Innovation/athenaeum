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
from functools import lru_cache
from pathlib import Path

import pytest
import yaml

from athenaeum.pii import scan_corpus_pii
from tests.evals.corpus import CORPUS_ROOT, build_corpus

# What counts as an identity worth guarding.
#
# A real knowledge tree titles pages with ordinary English nouns ("",
# "onboarding", "pricing") as readily as with real names. Those titles are not
# identities, and treating them as leaks makes the check fire constantly on
# nothing -- and a noisy guard is a guard somebody switches off. The first run
# of this test failed on exactly that: the word "retention".
#
# So the rule is about SHAPE, not a growing list of exceptions:
#   * multi-word names are always checked -- a person's full name, an org's
#     two-word name; that is where people and orgs live
#   * single tokens are checked unless they are common English words, since a
#     lowercase single-token identity (a repo or product name) is precisely
#     the case this corpus exists to test
# 4, not 5: several four-character surnames were live in this corpus and each
# collided with a real person page. A five-character floor excluded exactly
# the class of short surname most likely to collide.
_MIN_NAME_LEN = 4

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
    first format forward funnel future growth handbook hiring
    holiday impact income index insight intake invoice issue journey launch
    layout leave ledger legal lifecycle margin market meeting memory metric
    mission model month notes object offsite onboarding
    office allowance cadence threshold current former address standup
    output overview owner partner payment payroll pipeline planning playbook
    pricing principle priority process product profile program project
    proposal quality quarter queue rating record refund release
    remote report request research retention retro review router runbook
    salary sample schedule scope search service
    session since skill source sprint staffing standup status storage
    strategy stream summary support survey system taxonomy template
    testing their there these thing third those threshold timeline tooling
    traffic transfer under until update upgrade usage vendor version
    vision weekly where which while whose workflow would
    """.split()
)


def _knowledge_wiki() -> Path | None:
    root = Path(os.environ.get("ATHENAEUM_KNOWLEDGE_ROOT", Path.home() / "knowledge"))
    wiki = root / "wiki"
    return wiki if wiki.is_dir() else None


#: Page types whose `name` identifies somebody or something real. A page
#: titled "", "" or "here" is a topic note, not an
#: identity; including those made the untruncated scan fire on ordinary
#: English and would have got it switched off within a week.
_ENTITY_TYPES = frozenset({"person", "company", "project", "tool"})
_ENTITY_TYPE_LINE = re.compile(
    r"^type:\s*[\"']?(?:" + "|".join(sorted(_ENTITY_TYPES)) + r")[\"']?\s*$",
    re.MULTILINE,
)
_NAME_LINE = re.compile(r"^name:\s*(.+?)\s*$", re.MULTILINE)


@lru_cache(maxsize=4)
def _iter_frontmatter(wiki: Path) -> "tuple[dict, ...]":
    """Every page's frontmatter. The WHOLE tree -- no sampling, no limit.

    An earlier version took ``sorted(...)[:4000]``. Because pages are named by
    hex uid, that slice was not a random sample but "uids beginning 0, 1 or 2"
    -- 15% of the tree, deterministically the same 15% every run. Both leakage
    checks passed on a corpus that did contain a real person's surname and a
    real brand, because those pages sorted past the cutoff. A guarantee
    established by truncation is not a guarantee.

    Measured at 0.9s for 25,487 files, so the limit bought nothing it cost.

    Cached: both checks read the same scan, so the tree is walked once per
    session rather than once per test.
    """
    out: list[dict] = []
    for path in wiki.rglob("*.md"):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if not text.startswith("---"):
            continue
        end = text.find("\n---", 3)
        if end == -1:
            continue
        block = text[3:end]
        if not _ENTITY_TYPE_LINE.search(block):
            continue
        # Regex extraction, not yaml.safe_load: parsing 25k frontmatter blocks
        # costs ~60s and dominated the whole offline suite. Only `name` and
        # `aliases` are ever read, and both are simple scalars/lists here, so
        # a parser is not earning its cost.
        name = _NAME_LINE.search(block)
        entry: dict = {"name": name.group(1).strip().strip("\"'") if name else None}
        aliases: list[str] = []
        block_lines = block.splitlines()
        for i, line in enumerate(block_lines):
            if line.rstrip() == "aliases:":
                for follow in block_lines[i + 1 :]:
                    stripped = follow.strip()
                    if not stripped.startswith("- "):
                        break
                    aliases.append(stripped[2:].strip().strip("\"'"))
                break
        entry["aliases"] = aliases
        out.append(entry)
    return tuple(out)


def _real_entity_name_tokens(wiki: Path) -> set[str]:
    """Individual word tokens from the names of real PEOPLE, ORGS and PROJECTS.

    Tokenised, not whole-phrase: a real person's full name must be able to
    collide with a fixture character sharing only their surname, and a real
    two-word company name with a fixture that reuses one of its words.
    Whole-phrase matching made every such collision structurally invisible,
    which is how a real surname ended up as the corpus's central cast name.

    Real examples are deliberately NOT quoted here. This file is public, and
    naming the real person in a comment explaining why we stopped naming them
    would reproduce the leak inside its own fix -- which is exactly what
    happened once in the corpus README before the path lint's glob was widened
    to see .md files.

    Restricted to entity-typed pages so the set stays selective -- these are
    the names that identify somebody, as opposed to every word in the tree.
    """
    tokens: set[str] = set()
    for front in _iter_frontmatter(wiki):
        values = [front.get("name")]
        aliases = front.get("aliases")
        if isinstance(aliases, list):
            values.extend(aliases)
        for value in values:
            if isinstance(value, str):
                tokens.update(w for w in re.findall(r"[a-z][a-z'-]{2,}", value.lower()))
    return tokens


def _real_proper_nouns(wiki: Path) -> set[str]:
    """Collect ``name``/``aliases`` values from real wiki frontmatter.

    Frontmatter only, deliberately -- names are the leak that matters and the
    one extractable precisely. Scanning bodies would return mostly prose and
    drown the signal in false positives.
    """
    names: set[str] = set()
    for front in _iter_frontmatter(wiki):
        candidates = [front.get("name")]
        aliases = front.get("aliases")
        if isinstance(aliases, list):
            candidates.extend(aliases)
        for value in candidates:
            if not isinstance(value, str):
                continue
            cleaned = value.strip().lower()
            if len(cleaned) < _MIN_NAME_LEN or " " not in cleaned:
                # MULTI-WORD ONLY, deliberately. A single-token entity name is
                # indistinguishable from an ordinary English word without a
                # dictionary -- real entity pages here are named "Ford",
                # "Target", "Analytics", "Knowledge" -- so checking them here
                # produced a wall of false positives, and suppressing them with
                # a hand-maintained word list silently hid real companies
                # ("Target" is a real client). Neither is acceptable, so this
                # check takes the half it can do precisely, and single-token
                # identities are covered by the structural check below, which
                # only fires on names the corpus actually leans on.
                continue
            names.add(cleaned)
    return names


def _corpus_blobs() -> dict[str, str]:
    """Hand-authored sources plus a generated sample, keyed by label.

    Generated tiers are checked too: composed names can collide with a real
    name by chance, which is exactly why composition alone is not trusted.
    """
    # Every text file, not just *.yaml. The earlier glob missed README.md and
    # -- more to the point -- any `corpus-shape*.md` report, which is written
    # INTO this directory and embeds the absolute path of the source tree.
    # Only .gitignore stood between that file and the path lint that exists to
    # catch exactly it, and a lint that cannot see the likeliest offender is
    # not covering the case it claims to.
    blobs = {
        str(path.relative_to(CORPUS_ROOT)): path.read_text(encoding="utf-8")
        for path in sorted(CORPUS_ROOT.rglob("*"))
        if path.is_file() and path.suffix in {".yaml", ".yml", ".md", ".txt"}
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
    """Pin the division of labour between the two checks.

    This check takes MULTI-WORD names only, because a single-token entity name
    is indistinguishable from an ordinary English word without a dictionary --
    the real tree contains entity pages named "Ford", "Target", "Analytics".
    Checking them here produced a wall of false positives; suppressing them
    with a word list silently hid a real client. Single-token identities are
    the structural check's job, and only when the corpus leans on them.

    If someone later widens this to single tokens, this test fails and points
    at that trade-off rather than letting the noise return unexplained.
    """
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    for slug, name in [
        ("a", "Retention"),  # common word -> not an identity
        ("b", "Marisol Quenwick"),  # multi-word -> identity
        ("c", "zenodotus"),  # single lowercase token, not common -> identity
    ]:
        # `type: person` matters: only entity-typed pages are read, because a
        # page titled "analytics" or "here" is a topic note, not an identity.
        (wiki / f"{slug}.md").write_text(
            f"---\nuid: {slug}\ntype: person\nname: {name}\n---\n\nbody\n",
            encoding="utf-8",
        )

    found = _real_proper_nouns(wiki)
    assert "marisol quenwick" in found, "multi-word identities must be checked"
    assert "retention" not in found, "single common words must not be"
    assert "zenodotus" not in found, (
        "single tokens are deliberately NOT in this denylist -- they are "
        "covered by test_structurally_central_corpus_names_are_absent_from_"
        "the_real_tree, which fires only on names the corpus leans on"
    )


def test_structurally_central_corpus_names_are_absent_from_the_real_tree() -> None:
    """No name the corpus leans on may exist anywhere in the real tree.

    Scoped to STRUCTURALLY CENTRAL names -- those the corpus uses across three
    or more pages -- and that scoping is the whole design. The real tree holds
    ~17k person pages with ~11k distinct surnames, so *some* collision is
    unavoidable for any plausible invented name; failing on all of them would
    be unsatisfiable and the check would be switched off within a week.

    A name used once is incidental. A name the corpus builds a cast around --
    a person, their eponymous repo, their relatives, an org carrying the
    surname -- is one a reader could connect to a real individual, and that is
    the leak worth failing a build over.
    """
    wiki = _knowledge_wiki()
    if wiki is None:
        pytest.skip("no local knowledge tree -- cannot run (skipping loudly)")

    corpus = build_corpus(scale="core")
    counts: dict[str, int] = {}
    for page in corpus.pages:
        for token in {
            w.lower()
            for field in (page.name, *page.aliases)
            for w in re.findall(r"\b[A-Z][a-zA-Z]{3,}\b", field)
        }:
            counts[token] = counts.get(token, 0) + 1

    central = {t for t, n in counts.items() if n >= 3} - _COMMON_WORDS
    if not central:
        pytest.skip("no structurally central names to check")

    # Matched against the NAME TOKENS OF REAL ENTITY PAGES, not every word in
    # the tree. A 25k-page tree contains nearly every English word, so a
    # whole-tree token set flags "office" and teaches the reader to ignore
    # this test. Entity-name tokens are the ones that identify somebody.
    hits = sorted(central & _real_entity_name_tokens(wiki))
    assert not hits, (
        "names the eval corpus builds a cast around also appear in the local "
        f"knowledge tree: {hits}. Pick invented replacements and verify them "
        "against the tree BEFORE using them -- this corpus is public-bound."
    )


#: Well-known real brands that have no business in a public synthetic fixture.
#: A SHORT, curated list with a clear membership rule (a real, widely-known
#: commercial product or company), unlike a general "is this a company name?"
#: check -- the real tree holds 2,666 company/tool name tokens including
#: `about`, `access`, `data` and `first`, so matching against all of them
#: produced 170 hits that were almost entirely ordinary English.
#:
#: KNOWN LIMITATION, stated rather than papered over: a single-use,
#: single-token real brand NOT on this list cannot be caught mechanically.
#: The structural check needs three uses; matching every company name is
#: unusable noise. This list closes the realistic cases; the residue is a
#: review responsibility, not an automated guarantee.
_REAL_BRANDS = frozenset(
    """
    wework regus notion slack salesforce hubspot zoom dropbox asana trello jira
    confluence airtable figma miro stripe xero quickbooks netsuite workday
    greenhouse lever gusto zendesk intercom mailchimp shopify squarespace
    wordpress github gitlab bitbucket linkedin facebook twitter instagram
    aws azure heroku datadog snowflake databricks tableau
    """.split()
)


def test_every_composable_generated_name_is_invented() -> None:
    """Enumerate the WHOLE composable name space, not a sample.

    The generated tiers draw names from syllable pools, so the reachable set is
    finite and small -- roughly 150 given names x 90 surnames. Sampling one
    scale at one seed (which is all the corpus blob check does) leaves most of
    that space unexercised, and a collision would surface only when some future
    run happened to roll it.

    Enumerating instead makes the guarantee exhaustive and costs a fraction of
    a second, because it never generates a corpus at all.
    """
    wiki = _knowledge_wiki()
    if wiki is None:
        pytest.skip("no local knowledge tree -- cannot run (skipping loudly)")

    pools = yaml.safe_load(
        (CORPUS_ROOT / "templates" / "generated.yaml").read_text(encoding="utf-8")
    )["name_pools"]
    given = {(a + b).lower() for a in pools["given_syllables"] for b in pools["given_endings"]}
    surnames = {
        (a + b).lower() for a in pools["surname_syllables"] for b in pools["surname_endings"]
    }

    real_full = {n for n in _real_proper_nouns(wiki)}
    real_tokens = _real_entity_name_tokens(wiki)

    full_hits = sorted(f"{g} {s}" for g in given for s in surnames if f"{g} {s}" in real_full)
    assert not full_hits, f"composable names match real entities: {full_hits}"

    # Surnames are the identifying half; a composed given name colliding with
    # an ordinary first name is unavoidable and harmless.
    surname_hits = sorted(surnames & real_tokens)
    assert not surname_hits, (
        "composable SURNAMES collide with real entity names: "
        f"{surname_hits} -- adjust the syllable pools"
    )


def test_eval_corpus_names_no_real_commercial_brands() -> None:
    """A public fixture should not name real products or companies.

    Not a privacy leak so much as a truthfulness and neutrality one: the
    corpus README states every entity is invented, and placing an invented
    firm at a named real vendor implies a relationship that does not exist.

    Runs unconditionally -- no local knowledge tree needed -- so it protects
    outside contributors' PRs too.
    """
    hits: list[str] = []
    for label, blob in _corpus_blobs().items():
        tokens = {w.lower() for w in re.findall(r"\b[A-Za-z][A-Za-z'-]{2,}\b", blob)}
        for brand in sorted(tokens & _REAL_BRANDS):
            hits.append(f"{label}: {brand!r}")
    assert not hits, (
        "eval corpus names real commercial brands; replace them with invented "
        "equivalents:\n  " + "\n  ".join(hits)
    )


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
