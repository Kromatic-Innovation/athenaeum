# SPDX-License-Identifier: Apache-2.0
"""Tests for :mod:`athenaeum.pii_restore` (issue athenaeum#1037).

**Fixture-only verification** (one of this issue's own acceptance criteria):
every test here builds a synthetic corpus with its OWN throwaway git history
under ``tmp_path`` -- nothing touches ``~/knowledge`` or any live store, and
every root (``knowledge_root`` / ``wiki_root`` / ``contacts_root``) is passed
explicitly rather than defaulted.

The fixture built by :func:`_build_fixture_repo` carries, in one corpus, the
three cases athenaeum#1037's rename-following AC names plus the PII-safety cases:

- a **renamed page** whose pre-image exists only under its OLD path, one
  rename back (``renamed-page-old-name.md`` -> ``renamed-page-new-name.md``);
- a **reshaped (propagated-corruption) page** created AFTER the corruption
  existed, with no pre-image of its own (``reshaped-page.md``);
- a **retro-filename case** resolved by timestamp-key lookup into
  ``raw/retros/`` history, including AFTER the raw file has rotated out of
  the working tree (mirrors the real corpus's retention behaviour);
- a **genuine person email** and a **plausible phone number**, each on their
  own page, whose pre-images ARE findable but must never be restored (the
  email/phone-axis safety pin); and
- a page nested under an ``excluded/`` path component, which must never even
  become a scan candidate.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from athenaeum.pii_restore import (
    _RETRO_FRAGMENT_RE,
    MARKER,
    ApplyResult,
    PiiRestoreSafetyError,
    Restoration,
    RestorePlan,
    _classify_or_refuse,
    _is_excluded_path,
    apply_restore_plan,
    assert_excluded_population_unchanged,
    build_restore_plan,
    classify,
    classify_retro_issue_list,
    find_markers,
)

# --------------------------------------------------------------------------- #
# classify() golden fixture -- mirrors scripts/pii-restore.py's boundary
# (tests/test_pii_restore.py), pinned independently since this module's
# classify() is intentionally NOT imported from that dev-only script.
# --------------------------------------------------------------------------- #

GOLDEN_CASES: list[tuple[str, str, str | None]] = [
    ("empty_string", "", None),
    ("email_host_alias", "git@github.com", "email:host-alias/path"),
    ("email_service_id", "svc@my-proj.iam.gserviceaccount.com", "email:service-id"),
    ("email_role", "noreply@example.com", "email:role"),
    ("email_test_account", "qa-test@example.com", "email:test-account"),
    ("email_real_person_stays_redacted", "jane.doe@example.com", None),
    ("date_iso", "2026-08-14", "date:iso"),
    ("date_year_range", "2020-2023", "date:year-range"),
    ("isbn", "978-3-16-148410-0", "isbn"),
    ("decimal", ".75", "decimal"),
    ("id_fragment", "234567", "id-fragment"),
    ("date_embedded", "2025-08-26 05", "date:embedded"),
    # A double-dash separator is the (arbitrary but real) shape classify()
    # uses to tell "issue-number list" apart from "phone number" when both
    # would otherwise look like a bare digit-dash run of the same length --
    # see 801-835-841-843 below, which has the classic single-dash phone
    # shape and stays redacted for exactly that reason.
    ("number_list_double_dash", "691--720--683", "number-list"),
    ("number_other", "12 34 56", "number-other"),
    ("phone_like_stays_redacted", "555-123-4567", None),
    ("unrecognized_prose_stays_redacted", "some free text", None),
]


@pytest.mark.parametrize(
    "token,expected",
    [pytest.param(tok, exp, id=label) for label, tok, exp in GOLDEN_CASES],
)
def test_classify_golden_fixture(token: str, expected: str | None) -> None:
    assert classify(token) == expected


def test_classify_or_refuse_raises_on_real_pii_email() -> None:
    """PII safety pin: a genuine person address never reaches the write path."""
    with pytest.raises(PiiRestoreSafetyError):
        _classify_or_refuse("jane.doe@example.com", method="anchored-rename-follow")


def test_classify_or_refuse_raises_on_plausible_phone() -> None:
    """PII safety pin: the phone axis is refused the same as email."""
    with pytest.raises(PiiRestoreSafetyError):
        _classify_or_refuse("555-123-4567", method="anchored-rename-follow")


def test_classify_or_refuse_accepts_safe_class() -> None:
    assert _classify_or_refuse("2026-08-14", method="anchored-rename-follow") == "date:iso"


# --------------------------------------------------------------------------- #
# classify_retro_issue_list() -- the retro-filename method's own, narrower
# classifier (a digit-dash shape is unambiguous at THIS structural position,
# unlike the same shape found loose in prose).
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "value,expected",
    [
        ("801-835-841-843", "issue-list"),
        ("148", "issue-list"),
        ("691-720-683", "issue-list"),
        ("not-numeric", None),
        ("", None),
    ],
)
def test_classify_retro_issue_list(value: str, expected: str | None) -> None:
    assert classify_retro_issue_list(value) == expected


def test_classify_or_refuse_retro_method_accepts_single_dash_digit_list() -> None:
    """The exact shape that :func:`classify` refuses (phone ambiguity) IS
    restorable under the retro-filename method, because the ambiguity does
    not exist at that structural position."""
    assert (
        _classify_or_refuse("801-835-841-843", method="retro-filename-lookup")
        == "issue-list"
    )
    with pytest.raises(PiiRestoreSafetyError):
        _classify_or_refuse("801-835-841-843", method="anchored-rename-follow")


# --------------------------------------------------------------------------- #
# _RETRO_FRAGMENT_RE -- athenaeum#1107: the marker need not span the WHOLE
# ``--``...``.md`` region. Most live citations keep filename text (an
# issue-number prefix, a slug suffix, or both) around the marker; the
# pre-fix anchor (``--`` + MARKER + ``.md`` with nothing else permitted)
# only matched when that surrounding text happened to be empty.
# --------------------------------------------------------------------------- #

_TS = "20260612T214502Z"


@pytest.mark.parametrize(
    "case,line",
    [
        ("full_span", f"See retros/{_TS}--{MARKER}.md for details."),
        ("marker_at_start", f"See retros/{_TS}--{MARKER}-236config.md for details."),
        ("marker_at_end", f"See retros/{_TS}--athenaeum-1091-{MARKER}.md for details."),
        (
            "marker_in_middle",
            f"See retros/{_TS}--athenaeum-{MARKER}-236config.md for details.",
        ),
    ],
)
def test_retro_fragment_re_matches_marker_embedded_anywhere_in_span(
    case: str, line: str
) -> None:
    """athenaeum#1107 AC1/AC2: an embedded marker -- at the start, middle, or
    end of the ``--``...``.md`` span, not only spanning it entirely -- must
    still match and yield the correct timestamp key."""
    m = _RETRO_FRAGMENT_RE.search(line)
    assert m is not None, f"{case}: expected a match, got none for {line!r}"
    assert m.group("ts") == _TS


def test_retro_fragment_re_does_not_span_two_separate_citations_on_one_line() -> None:
    """Two DIFFERENT retro citations on one line must each be matched (or
    not) on their own -- the non-greedy, non-whitespace/non-slash-bounded
    filler must never let a match starting at the first citation's ``--``
    run all the way through to a MARKER that belongs to the second."""
    ts_a, ts_b = "20260101T000000Z", "20260202T000000Z"
    line = f"See retros/{ts_a}--clean-841.md and retros/{ts_b}--{MARKER}-843.md."
    m = _RETRO_FRAGMENT_RE.search(line)
    assert m is not None
    # Must anchor to the SECOND citation (the one that actually holds the
    # marker), never stretch from the first citation's "--" through to it.
    assert m.group("ts") == ts_b
    assert m.group(0).startswith(f"retros/{ts_b}--")


def test_retro_fragment_re_no_match_when_no_marker_present() -> None:
    """A clean (uncorrupted) retro citation must never match -- there is
    nothing to restore."""
    line = f"See retros/{_TS}--801-835-841-843.md for details."
    assert _RETRO_FRAGMENT_RE.search(line) is None


# --------------------------------------------------------------------------- #
# Fixture git repo
# --------------------------------------------------------------------------- #


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True
    )
    assert result.returncode == 0, f"git {args} failed: {result.stderr}"
    return result


def _commit(cwd: Path, message: str) -> str:
    _git(cwd, "add", "-A")
    _git(cwd, "commit", "-q", "-m", message)
    return _git(cwd, "rev-parse", "HEAD").stdout.strip()


LINKEDIN_CLEAN = "- **Last LinkedIn contact**: 2025-08-26 05:07:15 UTC (sent)[^1]\n"
LINKEDIN_CORRUPT = f"- **Last LinkedIn contact**: {MARKER}:07:15 UTC (sent)[^1]\n"
RETRO_CITE_CLEAN = "See retros/20260612T214502Z--801-835-841-843.md for details.\n"
RETRO_CITE_CORRUPT = f"See retros/20260612T214502Z--{MARKER}.md for details.\n"

#: The reshaped page's body: the copied corrupted line PLUS a large amount
#: of page-specific prose. The extra prose is load-bearing, not padding --
#: without it, this fixture's reshaped page and the renamed page share so
#: much of their content (frontmatter + the one copied line) that git's own
#: similarity-based rename detection (the default ``-M50%`` under
#: ``--follow``) mistakes the brand-new page for a rename of the OTHER page
#: and hands it that page's real history -- exactly the false positive a
#: real librarian-reshaped page (which carries plenty of its own unique
#: prose around a copied fragment) would not trigger.
RESHAPED_BODY = LINKEDIN_CORRUPT + (
    "\nThis page collects unrelated notes from a librarian split: budget "
    "planning, a roadmap sketch, and three paragraphs of meeting notes that "
    "share no history with the page the LinkedIn line was copied from. None "
    "of this prose existed before the reshape that created this page, so "
    "there is no earlier revision of THIS page to recover a pre-image from "
    "-- the pre-image, if any, belongs to the page this line was copied "
    "out of, and cross-page provenance is out of scope for this tool.\n"
    "\nSecond paragraph: further padding prose distinguishing this page's "
    "content from any other fixture page, well past git's default rename-"
    "similarity threshold.\n"
    "\nThird paragraph: same purpose, different words, unrelated topic "
    "entirely -- quarterly numbers, a status update, and a closing note.\n"
)


def _page(uid: str, name: str, body: str) -> str:
    return f'---\nuid: "{uid}"\nname: {name}\ntype: person\n---\n{body}'


def _retro_citing_page(uid: str, name: str, citation_line: str) -> str:
    """A page citing a retro filename from its ``description:`` frontmatter
    field (rather than the body) -- the field FTS5 actually indexes
    (:data:`athenaeum.search.FTS5Backend._CREATE_SQL`'s ``description``
    column), so the CLI-level reindex/recall test has something real to
    query. :mod:`athenaeum.pii_restore` treats frontmatter and body lines
    identically (:func:`athenaeum.pii_restore.find_markers` scans the whole
    file text), so this is not a special case for the tool under test --
    only a choice of WHERE this fixture puts its retro-filename marker.
    """
    return (
        f'---\nuid: "{uid}"\nname: {name}\ntype: person\n'
        f'description: "{citation_line.strip()}"\n---\n(see footnote)\n'
    )


def _build_fixture_repo(tmp_path: Path) -> Path:
    """Build the synthetic corpus described in this module's docstring.

    Returns the repo root (== knowledge root). ``wiki/`` and
    ``excluded/`` (the contacts surface, ``storage.mapping: {pii: excluded}``)
    are subdirectories of it.
    """
    root = tmp_path / "knowledge"
    wiki = root / "wiki"
    retros = root / "raw" / "retros"
    # Matches the real ``storage.mapping: {pii: excluded}`` resolution
    # (:func:`athenaeum.storage.surface_root_for_class`): the excluded
    # adapter's surface root is ``<knowledge_root>/excluded`` FLAT -- no
    # per-class subdirectory. Kept in sync with the ``athenaeum.yaml`` this
    # fixture writes below so the CLI's own config-resolved contacts_root
    # (issue athenaeum#1037's CLI tests) agrees with the one library-level
    # tests pass explicitly.
    contacts = root / "excluded"
    for d in (wiki, retros, contacts, wiki / "excluded"):
        d.mkdir(parents=True, exist_ok=True)

    _git(root, "init", "-q", "-b", "develop")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Athenaeum Test")

    (root / "athenaeum.yaml").write_text(
        "storage:\n  mapping:\n    pii: excluded\n"
    )

    # Pre-existing migrated contact records -- the population this tool must
    # never move.
    (contacts / "person-a-record.md").write_text("uid: 1002\nemail: jane.doe@example.com\n")
    (contacts / "person-b-record.md").write_text("uid: 1003\nphone: 555-123-4567\n")

    # -- commit 1: clean corpus, pre-migration --------------------------- #
    (wiki / "renamed-page-old-name.md").write_text(
        _page("1001", "Renamed Person", LINKEDIN_CLEAN)
    )
    (wiki / "person-a.md").write_text(_page("1002", "Person A", "Email: jane.doe@example.com\n"))
    (wiki / "person-b.md").write_text(_page("1003", "Person B", "Phone: 555-123-4567\n"))
    (wiki / "retro-citing-page.md").write_text(
        _retro_citing_page("1004", "Retro Citer", RETRO_CITE_CLEAN)
    )
    (wiki / "excluded" / "decoy.md").write_text(_page("1006", "Decoy", "nothing to see\n"))
    (retros / "20260612T214502Z--801-835-841-843.md").write_text("# Retro\n\nBody text.\n")
    _commit(root, "initial: seed clean corpus")

    # -- commit 2: the migration corrupts markers ------------------------ #
    (wiki / "renamed-page-old-name.md").write_text(
        _page("1001", "Renamed Person", LINKEDIN_CORRUPT)
    )
    (wiki / "person-a.md").write_text(_page("1002", "Person A", f"Email: {MARKER}\n"))
    (wiki / "person-b.md").write_text(_page("1003", "Person B", f"Phone: {MARKER}\n"))
    (wiki / "retro-citing-page.md").write_text(
        _retro_citing_page("1004", "Retro Citer", RETRO_CITE_CORRUPT)
    )
    (wiki / "excluded" / "decoy.md").write_text(_page("1006", "Decoy", f"{MARKER}\n"))
    _commit(root, "storage: migrate contact data to the excluded surface")

    # -- commit 3: rename (still corrupted; no content change) ----------- #
    _git(
        root,
        "mv",
        "wiki/renamed-page-old-name.md",
        "wiki/renamed-page-new-name.md",
    )
    _commit(root, "librarian: move-then-retire")

    # -- commit 4: a reshape copies already-corrupted prose into a NEW page #
    (wiki / "reshaped-page.md").write_text(_page("1005", "Reshaped Page", RESHAPED_BODY))
    _commit(root, "librarian: split reshapes existing prose into a new page")

    # -- commit 5: the raw retro rotates out of the working tree --------- #
    _git(root, "rm", "-q", "raw/retros/20260612T214502Z--801-835-841-843.md")
    _commit(root, "raw: rotate retro out of working tree")

    return root


@pytest.fixture
def fixture_repo(tmp_path: Path) -> Path:
    return _build_fixture_repo(tmp_path)


def _roots(repo_root: Path) -> tuple[Path, Path]:
    return repo_root / "wiki", repo_root / "excluded"


# --------------------------------------------------------------------------- #
# find_markers -- excluded/ is never even a candidate
# --------------------------------------------------------------------------- #


def test_find_markers_never_returns_hits_under_excluded_path_component(
    fixture_repo: Path,
) -> None:
    wiki_root, contacts_root = _roots(fixture_repo)
    hits = find_markers(wiki_root, contacts_root)
    assert all("excluded" not in Path(h.page_relpath).parts for h in hits)


def test_find_markers_finds_every_scanned_page(fixture_repo: Path) -> None:
    wiki_root, contacts_root = _roots(fixture_repo)
    hits = find_markers(wiki_root, contacts_root)
    pages = {h.page_relpath for h in hits}
    assert pages == {
        "wiki/renamed-page-new-name.md",
        "wiki/person-a.md",
        "wiki/person-b.md",
        "wiki/retro-citing-page.md",
        "wiki/reshaped-page.md",
    }


# --------------------------------------------------------------------------- #
# build_restore_plan -- the three AC5 fixture cases + the PII-safety cases
# --------------------------------------------------------------------------- #


def test_renamed_page_restored_via_anchored_rename_follow(fixture_repo: Path) -> None:
    """AC5 case 1: rename-following recovers a pre-image one rename back."""
    wiki_root, contacts_root = _roots(fixture_repo)
    plan = build_restore_plan(fixture_repo, wiki_root, contacts_root)
    matches = [
        r for r in plan.restorations if r.hit.page_relpath == "wiki/renamed-page-new-name.md"
    ]
    assert len(matches) == 1
    restoration = matches[0]
    assert restoration.method == "anchored-rename-follow"
    assert restoration.restore_class == "date:embedded"
    assert restoration.token == "2025-08-26 05"


def test_reshaped_page_reported_as_honest_residue(fixture_repo: Path) -> None:
    """AC5 case 2: a page created after the corruption existed (no pre-image
    of its own) is named residue, never guessed at via cross-page tracing."""
    wiki_root, contacts_root = _roots(fixture_repo)
    plan = build_restore_plan(fixture_repo, wiki_root, contacts_root)
    matches = [e for e in plan.residue if e.hit.page_relpath == "wiki/reshaped-page.md"]
    assert len(matches) == 1
    assert matches[0].reason == "no-pre-image:page-created-after-migration"
    # And it must NOT also appear as a restoration.
    assert not any(
        r.hit.page_relpath == "wiki/reshaped-page.md" for r in plan.restorations
    )


def test_retro_filename_resolved_by_history_lookup_after_rotation(fixture_repo: Path) -> None:
    """AC5 case 3: the retro-filename class resolves even after the raw file
    has rotated out of the working tree (commit 5), because the lookup is
    keyed on git-add history, not the current tree."""
    wiki_root, contacts_root = _roots(fixture_repo)
    plan = build_restore_plan(fixture_repo, wiki_root, contacts_root)
    matches = [
        r for r in plan.restorations if r.hit.page_relpath == "wiki/retro-citing-page.md"
    ]
    assert len(matches) == 1
    restoration = matches[0]
    assert restoration.method == "retro-filename-lookup"
    assert restoration.token == "801-835-841-843"
    assert restoration.restore_class == "issue-list"
    assert restoration.source_ref == "20260612T214502Z--801-835-841-843.md"


@pytest.mark.parametrize(
    "citation_line",
    [
        pytest.param(
            f"See retros/{_TS}--{MARKER}-236config.md for details.\n",
            id="marker_at_start_of_span",
        ),
        pytest.param(
            f"See retros/{_TS}--athenaeum-{MARKER}-236config.md for details.\n",
            id="marker_in_middle_of_span",
        ),
        pytest.param(
            f"See retros/{_TS}--athenaeum-1091-{MARKER}.md for details.\n",
            id="marker_at_end_of_span",
        ),
    ],
)
def test_retro_filename_resolved_when_marker_is_embedded_not_full_span(
    tmp_path: Path, citation_line: str
) -> None:
    """athenaeum#1107 AC1/AC2: end-to-end proof that a corrupted citation
    with the marker embedded partway through the ``--``...``.md`` span --
    not spanning it entirely -- still resolves through the full
    match -> resolve -> classify -> restore pipeline. Before the fix,
    :data:`_RETRO_FRAGMENT_RE` never matched any of these shapes, so
    :func:`build_restore_plan` routed them to :func:`_plan_anchored_restore`
    instead (the wrong method, and one with no pre-image to find), leaving
    them stuck as residue rather than restored via the retro-filename
    lookup."""
    root = tmp_path / "knowledge"
    wiki = root / "wiki"
    retros = root / "raw" / "retros"
    contacts = root / "excluded"
    for d in (wiki, retros, contacts):
        d.mkdir(parents=True, exist_ok=True)

    _git(root, "init", "-q", "-b", "develop")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Athenaeum Test")
    (root / "athenaeum.yaml").write_text("storage:\n  mapping:\n    pii: excluded\n")

    real_filename = f"{_TS}--801-835-841-843.md"
    (wiki / "citing-page.md").write_text(
        _page("2001", "Embedded Marker Citer", f"See retros/{_TS}--801-835-841-843.md.\n")
    )
    (retros / real_filename).write_text("# Retro\n\nBody text.\n")
    _commit(root, "initial: seed clean corpus")

    (wiki / "citing-page.md").write_text(
        _page("2001", "Embedded Marker Citer", citation_line)
    )
    _commit(root, "storage: migrate contact data to the excluded surface")

    wiki_root, contacts_root = root / "wiki", root / "excluded"
    plan = build_restore_plan(root, wiki_root, contacts_root)

    matches = [r for r in plan.restorations if r.hit.page_relpath == "wiki/citing-page.md"]
    assert len(matches) == 1, (
        f"expected the embedded-marker citation to resolve; residue was "
        f"{[e.reason for e in plan.residue if e.hit.page_relpath == 'wiki/citing-page.md']}"
    )
    restoration = matches[0]
    assert restoration.method == "retro-filename-lookup"
    assert restoration.token == "801-835-841-843"
    assert restoration.restore_class == "issue-list"
    assert restoration.source_ref == real_filename


@pytest.mark.parametrize(
    "page_relpath,token",
    [
        ("wiki/person-a.md", "jane.doe@example.com"),
        ("wiki/person-b.md", "555-123-4567"),
    ],
)
def test_genuine_pii_pre_image_stays_residue_never_restored(
    fixture_repo: Path, page_relpath: str, token: str
) -> None:
    """PII safety pin: a discoverable pre-image that classifies as real
    person contact data (email OR phone axis) is never a restoration."""
    wiki_root, contacts_root = _roots(fixture_repo)
    plan = build_restore_plan(fixture_repo, wiki_root, contacts_root)
    assert not any(r.hit.page_relpath == page_relpath for r in plan.restorations)
    matches = [e for e in plan.residue if e.hit.page_relpath == page_relpath]
    assert len(matches) == 1
    assert matches[0].reason == "kept:real-pii"


def test_full_plan_shape(fixture_repo: Path) -> None:
    """Every marker :func:`find_markers` finds lands in exactly one of
    restorations/residue -- no third, unreported bucket."""
    wiki_root, contacts_root = _roots(fixture_repo)
    plan = build_restore_plan(fixture_repo, wiki_root, contacts_root)
    assert plan.pages_scanned == 5
    assert len(plan.restorations) == 2
    assert len(plan.residue) == 3
    counts = plan.residue_counts_by_reason()
    assert counts["kept:real-pii"] == 2
    assert counts["no-pre-image:page-created-after-migration"] == 1


def test_limit_caps_hits_scanned(fixture_repo: Path) -> None:
    wiki_root, contacts_root = _roots(fixture_repo)
    plan = build_restore_plan(fixture_repo, wiki_root, contacts_root, limit=1)
    assert len(plan.restorations) + len(plan.residue) == 1


# --------------------------------------------------------------------------- #
# apply_restore_plan -- writes, reindex-adjacent count pin, refusals
# --------------------------------------------------------------------------- #


def test_apply_restores_text_and_leaves_residue_pages_untouched(fixture_repo: Path) -> None:
    wiki_root, contacts_root = _roots(fixture_repo)
    plan = build_restore_plan(fixture_repo, wiki_root, contacts_root)
    result = apply_restore_plan(plan, wiki_root=wiki_root, contacts_root=contacts_root)

    assert isinstance(result, ApplyResult)
    assert result.pages_changed == 2
    assert result.sites_restored == 2

    renamed_text = (wiki_root / "renamed-page-new-name.md").read_text()
    assert LINKEDIN_CLEAN in renamed_text
    assert MARKER not in renamed_text

    retro_text = (wiki_root / "retro-citing-page.md").read_text()
    assert RETRO_CITE_CLEAN.strip() in retro_text
    assert MARKER not in retro_text

    # Residue pages are byte-for-byte untouched.
    assert MARKER in (wiki_root / "person-a.md").read_text()
    assert MARKER in (wiki_root / "person-b.md").read_text()
    assert MARKER in (wiki_root / "reshaped-page.md").read_text()


def test_apply_dry_run_never_writes(fixture_repo: Path) -> None:
    """Dry-run default: build_restore_plan alone must never touch disk."""
    wiki_root, contacts_root = _roots(fixture_repo)
    before = (wiki_root / "renamed-page-new-name.md").read_text()
    build_restore_plan(fixture_repo, wiki_root, contacts_root)
    after = (wiki_root / "renamed-page-new-name.md").read_text()
    assert before == after
    assert MARKER in after


def test_apply_pins_migrated_address_population_before_and_after(fixture_repo: Path) -> None:
    """AC: a count assertion verifies the migrated-address population is
    untouched before and after apply -- checked here against the ACTUAL
    :class:`ApplyResult`, not just re-derived independently by the test."""
    wiki_root, contacts_root = _roots(fixture_repo)
    plan = build_restore_plan(fixture_repo, wiki_root, contacts_root)
    result = apply_restore_plan(plan, wiki_root=wiki_root, contacts_root=contacts_root)
    assert result.pre_count == 2
    assert result.post_count == 2
    # And the records themselves are byte-identical -- not just same count.
    person_a_record = "uid: 1002\nemail: jane.doe@example.com\n"
    person_b_record = "uid: 1003\nphone: 555-123-4567\n"
    assert (contacts_root / "person-a-record.md").read_text() == person_a_record
    assert (contacts_root / "person-b-record.md").read_text() == person_b_record


def test_assert_excluded_population_unchanged_raises_on_drift() -> None:
    with pytest.raises(PiiRestoreSafetyError):
        assert_excluded_population_unchanged(before=2, after=1)
    with pytest.raises(PiiRestoreSafetyError):
        assert_excluded_population_unchanged(before=2, after=3)
    assert_excluded_population_unchanged(before=2, after=2)  # does not raise


def test_apply_raises_if_population_count_drifts_mid_run(
    fixture_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The count assertion is a REAL pre/post check inside apply_restore_plan
    itself, not only something the test re-derives: force the second
    ``iter_contact_records`` call (the post-write recount) to disagree with
    the first, and confirm the apply path raises rather than reporting
    success."""
    import athenaeum.pii_restore as pii_restore_mod

    wiki_root, contacts_root = _roots(fixture_repo)
    plan = build_restore_plan(fixture_repo, wiki_root, contacts_root)

    real_iter = pii_restore_mod.iter_contact_records
    calls = {"n": 0}

    def _flaky(root: Path) -> list[Path]:
        calls["n"] += 1
        result = real_iter(root)
        if calls["n"] >= 2:
            return result[:0]  # simulate a record vanishing off the surface
        return result

    monkeypatch.setattr(pii_restore_mod, "iter_contact_records", _flaky)
    with pytest.raises(PiiRestoreSafetyError):
        apply_restore_plan(plan, wiki_root=wiki_root, contacts_root=contacts_root)


def test_apply_over_restore_attempt_is_refused_regardless_of_stored_class(
    fixture_repo: Path,
) -> None:
    """PII safety pin, the AC's own wording: 'an over-restore attempt on a
    fixture asserts refusal.' Construct a plan BY HAND that tries to
    restore a genuine person email under a fabricated safe-looking class
    label -- apply_restore_plan re-derives the classification from the
    TOKEN itself, so a tampered label cannot bypass the refusal."""
    wiki_root, contacts_root = _roots(fixture_repo)
    hits = find_markers(wiki_root, contacts_root)
    person_a_hit = next(h for h in hits if h.page_relpath == "wiki/person-a.md")
    forged = Restoration(
        hit=person_a_hit,
        token="jane.doe@example.com",
        restore_class="date:iso",  # forged -- not what classify() would say
        method="anchored-rename-follow",
        source_ref="deadbeef",
    )
    plan = RestorePlan(pages_scanned=1, restorations=[forged], residue=[])
    with pytest.raises(PiiRestoreSafetyError):
        apply_restore_plan(plan, wiki_root=wiki_root, contacts_root=contacts_root)
    # And nothing was written -- the refusal fires before any page touches disk.
    assert MARKER in (wiki_root / "person-a.md").read_text()


def test_apply_refuses_to_write_under_excluded_surface(fixture_repo: Path) -> None:
    """PII safety pin: the tool never writes under ``excluded/``, enforced
    even if a (hypothetical, hand-built) plan names a target there."""
    wiki_root, contacts_root = _roots(fixture_repo)
    hits = find_markers(wiki_root, contacts_root)
    person_a_hit = hits[0]
    forged_hit = person_a_hit.__class__(
        page_relpath="excluded/forged.md",
        line_no=person_a_hit.line_no,
        char_offset=person_a_hit.char_offset,
        line_text=person_a_hit.line_text,
    )
    forged = Restoration(
        hit=forged_hit,
        token="2026-08-14",
        restore_class="date:iso",
        method="anchored-rename-follow",
        source_ref="deadbeef",
    )
    plan = RestorePlan(pages_scanned=1, restorations=[forged], residue=[])
    with pytest.raises(PiiRestoreSafetyError, match="excluded surface"):
        apply_restore_plan(plan, wiki_root=wiki_root, contacts_root=contacts_root)


def test_is_excluded_path_matches_contacts_root_and_path_component(
    fixture_repo: Path,
) -> None:
    wiki_root, contacts_root = _roots(fixture_repo)
    assert _is_excluded_path(contacts_root / "some-record.md", contacts_root)
    assert _is_excluded_path(wiki_root / "excluded" / "decoy.md", contacts_root)
    assert not _is_excluded_path(wiki_root / "person-a.md", contacts_root)
