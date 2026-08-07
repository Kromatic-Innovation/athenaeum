# SPDX-License-Identifier: Apache-2.0
"""Tests for PII off-corpus (issue athenaeum#427): excluded contacts surface, entity-page
lint, and the append-only observation log + supersession fold.

Structure mirrors the issue's acceptance criteria:

- ``TestExcludedSurfaceFailsClosed`` — a page on the contacts (excluded)
  surface never appears in embeddings (vector), FTS5 recall, keyword recall,
  or merge proposals. One test per consumer, proving the exclusion is
  inherited BY CONSTRUCTION through athenaeum#429's adapter interface (fail-closed) —
  no athenaeum#427-specific code path in the consumer, just the adapter's excluded
  surface root sitting outside the scanned tree.
- ``TestPiiFlagBeltAndSuspenders`` — a ``pii: true``-flagged page (still on
  the default wiki surface) is ALSO excluded from every consumer.
- ``TestEntityPageLint`` — inline ``emails``/``phones`` frontmatter and
  inline body text are flagged, and the flag is silenced by ``pii: true``.
- ``TestObservationLog`` — append/read/supersession/fold, including the
  shared-address (multi-person) read and the Jason/Janice correction shape.
"""

from __future__ import annotations

import warnings
from pathlib import Path

import pytest

from athenaeum.pii import (
    HardBounceFact,
    Observation,
    Supersession,
    append_observation,
    append_supersession,
    contacts_surface_root,
    default_bounce_record_path,
    detect_hard_bounce_fact,
    find_inline_emails,
    find_inline_phones,
    fold_observations,
    has_inline_contact_fields,
    is_bounced,
    is_pii_class_excluded,
    is_pii_flagged,
    lint_inline_contact_fields,
    mark_bounced,
    read_bounce_record,
    read_observations,
    read_supersessions,
    resolve_identifier,
    scan_corpus_pii,
)
from athenaeum.schemas import PersonWiki, validate_wiki_meta
from athenaeum.search import FTS5Backend, KeywordBackend
from athenaeum.storage import surface_root_for_class
from athenaeum.wiki_dedupe import discover_wiki_dedupe_candidates

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

EXCLUDED_CONFIG = {"storage": {"mapping": {"pii": "excluded"}}}


def _write_page(
    root: Path, filename: str, *, page_type: str, name: str, body: str, extra: str = ""
) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / filename
    path.write_text(
        f"---\nuid: {filename[:-3]}\nname: {name}\ntype: {page_type}\n{extra}---\n{body}\n",
        encoding="utf-8",
    )
    return path


# ---------------------------------------------------------------------------
# Excluded surface — fail-closed by construction (one test per consumer)
# ---------------------------------------------------------------------------


class TestExcludedSurfaceFailsClosed:
    def _build_knowledge(self, tmp_path: Path) -> tuple[Path, Path, Path]:
        """Return (knowledge_root, wiki_root, contacts_root) with one public
        page in wiki/ and one contact page on the excluded surface."""
        knowledge_root = tmp_path / "knowledge"
        wiki_root = knowledge_root / "wiki"
        _write_page(
            wiki_root,
            "alice-public.md",
            page_type="concept",
            name="Alice Public Page",
            body="Alice is a public concept page about Lean Startup.",
        )
        contacts_root = contacts_surface_root(knowledge_root, EXCLUDED_CONFIG)
        _write_page(
            contacts_root,
            "alice-contact.md",
            page_type="pii",
            name="Alice Contact",
            body="alice@example.com +1-555-0100",
        )
        return knowledge_root, wiki_root, contacts_root

    def test_contacts_surface_root_is_outside_wiki(self, tmp_path: Path) -> None:
        knowledge_root = tmp_path / "knowledge"
        contacts_root = contacts_surface_root(knowledge_root, EXCLUDED_CONFIG)
        wiki_root = knowledge_root / "wiki"
        assert contacts_root == knowledge_root / "excluded"
        assert wiki_root not in contacts_root.parents
        assert is_pii_class_excluded(EXCLUDED_CONFIG)

    def test_unconfigured_pii_class_defaults_to_wiki(self, tmp_path: Path) -> None:
        # No storage.mapping => byte-identical default: pii resolves to the
        # ordinary wiki surface, matching athenaeum#429's "unconfigured = default" rule.
        knowledge_root = tmp_path / "knowledge"
        assert contacts_surface_root(knowledge_root, None) == knowledge_root / "wiki"
        assert not is_pii_class_excluded(None)

    def test_excluded_from_fts5_build_and_query(self, tmp_path: Path) -> None:
        knowledge_root, wiki_root, _contacts_root = self._build_knowledge(tmp_path)
        cache_dir = tmp_path / "cache"
        backend = FTS5Backend()
        backend.build_index(wiki_root, cache_dir)
        hits = backend.query("alice", cache_dir, n=10)
        # The contacts-surface page was never scanned, so it can't be a hit —
        # only the public wiki page (which also happens to mention "Alice"
        # nowhere, so zero hits is the expected/safe outcome for that probe).
        assert all("alice-contact" not in fname for fname, _name, _score in hits)
        # Direct proof the excluded root was never part of the scanned set.
        from athenaeum.search import _scan_all_entries

        scanned = {name for name, _p in _scan_all_entries(wiki_root, None)}
        assert "alice-public.md" in scanned
        assert not any("alice-contact" in n for n in scanned)

    def test_excluded_from_vector_build(self, tmp_path: Path) -> None:
        pytest.importorskip("chromadb")
        from athenaeum.search import VectorBackend

        knowledge_root, wiki_root, _contacts_root = self._build_knowledge(tmp_path)
        cache_dir = tmp_path / "cache"
        backend = VectorBackend()
        count = backend.build_index(wiki_root, cache_dir)
        assert count == 1  # only alice-public.md — the excluded page never scanned
        hits = backend.query("alice contact phone email", cache_dir, n=10)
        assert all("alice-contact" not in doc_id for doc_id, _name, _dist in hits)

    def test_excluded_from_keyword_recall(self, tmp_path: Path) -> None:
        knowledge_root, wiki_root, _contacts_root = self._build_knowledge(tmp_path)
        backend = KeywordBackend()
        hits = backend.query("alice contact phone email", Path("unused"), wiki_root=wiki_root, n=10)
        assert all("alice-contact" not in fname for fname, _name, _score in hits)

    def test_excluded_from_merge_proposals(self, tmp_path: Path) -> None:
        knowledge_root = tmp_path / "knowledge"
        wiki_root = knowledge_root / "wiki"
        _write_page(wiki_root, "a.md", page_type="concept", name="A", body="a")
        _write_page(wiki_root, "b.md", page_type="reference", name="B", body="b")
        contacts_root = contacts_surface_root(knowledge_root, EXCLUDED_CONFIG)
        _write_page(
            contacts_root,
            "alice-contact.md",
            page_type="pii",
            name="Alice Contact",
            body="alice@example.com",
        )
        # discover_wiki_dedupe_candidates only ever globs wiki_root itself, so
        # the excluded-surface file (living outside wiki/) is never even a
        # glob candidate — by construction, not a athenaeum#427-specific filter.
        names = {
            c.path.name
            for c in discover_wiki_dedupe_candidates(wiki_root, config=EXCLUDED_CONFIG)
        }
        assert "alice-contact.md" not in names
        assert names == {"a.md", "b.md"}


# ---------------------------------------------------------------------------
# pii: true — belt-and-suspenders exclusion for an in-corpus page
# ---------------------------------------------------------------------------


class TestPiiFlagBeltAndSuspenders:
    def test_is_pii_flagged_coercion(self) -> None:
        assert is_pii_flagged({"pii": True})
        assert is_pii_flagged({"pii": "true"})
        assert is_pii_flagged({"pii": "YES"})
        assert not is_pii_flagged({"pii": False})
        assert not is_pii_flagged({"pii": "false"})
        assert not is_pii_flagged({})
        assert not is_pii_flagged(None)
        # Non-bool/non-string values are not coerced (mirrors is_pointer_stub).
        assert not is_pii_flagged({"pii": 1})

    def test_flagged_page_excluded_from_fts5(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        _write_page(
            wiki_root,
            "bob.md",
            page_type="person",
            name="Bob",
            body="Bob notes.",
            extra="pii: true\n",
        )
        _write_page(wiki_root, "carol.md", page_type="person", name="Carol", body="Carol notes.")
        cache_dir = tmp_path / "cache"
        backend = FTS5Backend()
        count = backend.build_index(wiki_root, cache_dir)
        assert count == 1
        from athenaeum.search import _scan_all_entries

        scanned = {name for name, _p in _scan_all_entries(wiki_root, None)}
        # _scan_all_entries itself doesn't filter pii (that happens in
        # _scan_indexed_records) — confirm via the actual indexed record scan.
        from athenaeum.search import _scan_indexed_records

        recs = list(_scan_indexed_records(wiki_root, None))
        names = {n for n, _p, _h, _t, _m, _s in recs}
        assert "bob.md" not in names
        assert "carol.md" in names
        assert "bob.md" in scanned  # present on disk, just excluded from the index

    def test_flagged_page_excluded_from_keyword_recall(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        _write_page(
            wiki_root,
            "bob.md",
            page_type="person",
            name="Bob",
            body="Bob notes with a searchable marker XYZZY.",
            extra="pii: true\n",
        )
        backend = KeywordBackend()
        hits = backend.query("XYZZY", Path("unused"), wiki_root=wiki_root, n=10)
        assert hits == []

    def test_flagged_page_excluded_from_merge_candidates(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        _write_page(
            wiki_root, "a.md", page_type="concept", name="A", body="a", extra="pii: true\n"
        )
        _write_page(wiki_root, "b.md", page_type="concept", name="B", body="b")
        names = {c.path.name for c in discover_wiki_dedupe_candidates(wiki_root)}
        assert names == {"b.md"}


# ---------------------------------------------------------------------------
# Entity-page lint — inline emails/phones flagged
# ---------------------------------------------------------------------------


class TestEntityPageLint:
    def test_find_inline_emails(self) -> None:
        assert find_inline_emails("reach alice@example.com or bob@test.co") == [
            "alice@example.com",
            "bob@test.co",
        ]
        assert find_inline_emails("no email here") == []

    def test_find_inline_phones(self) -> None:
        assert find_inline_phones("call +1-555-0100 now") == ["+1-555-0100"]
        assert find_inline_phones("(555) 010-0100") == ["(555) 010-0100"]
        assert find_inline_phones("issue athenaeum#427 page 12") == []

    def test_has_inline_contact_fields_frontmatter(self) -> None:
        assert has_inline_contact_fields({"emails": ["a@example.com"]})
        assert has_inline_contact_fields({"phones": ["+1-555-0100"]})
        assert not has_inline_contact_fields({"name": "Alice", "linkedin_url": "https://x"})

    def test_has_inline_contact_fields_body(self) -> None:
        assert has_inline_contact_fields({}, "email me at alice@example.com")
        assert has_inline_contact_fields({}, "call +1-555-0100")
        assert not has_inline_contact_fields({}, "durable identifier only, no contact info")

    def test_lint_message_names_file_and_reason(self, tmp_path: Path) -> None:
        msg = lint_inline_contact_fields(
            {"emails": ["a@example.com"]}, "", Path("/wiki/alice.md")
        )
        assert msg is not None
        assert "/wiki/alice.md" in msg
        assert "emails" in msg

    def test_lint_silent_when_no_contact_data(self) -> None:
        assert lint_inline_contact_fields({"name": "Alice", "linkedin_url": "https://x"}) is None

    def test_pydantic_warns_on_inline_emails(self) -> None:
        meta = {
            "uid": "person-alice",
            "type": "person",
            "name": "Alice",
            "emails": ["alice@example.com"],
        }
        with pytest.warns(UserWarning, match="inline contact data"):
            validate_wiki_meta(meta)

    def test_pydantic_warns_on_inline_phones(self) -> None:
        meta = {
            "uid": "person-alice",
            "type": "person",
            "name": "Alice",
            "phones": ["+1-555-0100"],
        }
        with pytest.warns(UserWarning, match="inline contact data"):
            validate_wiki_meta(meta)

    def test_pydantic_silent_for_durable_identifiers_only(self) -> None:
        meta = {
            "uid": "person-alice",
            "type": "person",
            "name": "Alice",
            "linkedin_url": "https://www.linkedin.com/in/alice",
            "apollo_id": "apollo_123",
        }
        with warnings.catch_warnings():
            warnings.simplefilter("error", UserWarning)
            model = validate_wiki_meta(meta)
        assert isinstance(model, PersonWiki)

    def test_pii_true_silences_the_pydantic_warning(self) -> None:
        meta = {
            "uid": "person-alice",
            "type": "person",
            "name": "Alice",
            "emails": ["alice@example.com"],
            "pii": True,
        }
        with warnings.catch_warnings():
            warnings.simplefilter("error", UserWarning)
            validate_wiki_meta(meta)  # must not raise/warn


# ---------------------------------------------------------------------------
# Phone detector — corpus false positives must NOT match (issue athenaeum#500)
# ---------------------------------------------------------------------------
#
# The permissive phone regex matched ISO dates, year ranges, and bare id/
# analytics fragments — confirmed 2026-07-28 against the live corpus, where a
# lint-pii pass flagged 1,885 pages on "phone" hits dominated by CRM-timeline
# dates (e.g. `2015-12-03`) and id fragments (page uid prefixes, GA4 property
# ids). These are the concrete corpus samples from athenaeum#500, pinned as non-matches
# alongside the true-positive fixtures so the detector cannot regress.


class TestPhoneDetectorFalsePositives:
    #: The exact "phone" hits migrate-pii reported on the two live pages named
    #: in athenaeum#500 — all CRM-timeline / frontmatter dates, no real phone numbers.
    ISO_DATE_FALSE_POSITIVES = (
        "2015-12-03",  # blekinge page: "First contact"
        "2021-07-16",  # blekinge page: "Last CRM update"
        "2016-03-14",  # blekinge page: "Last email"
        "2026-04-16",  # dawn-b page: its own `updated:` frontmatter date
    )

    #: Bare digit runs athenaeum#500 calls out: a page uid prefix and a GA4 property id.
    ID_FRAGMENT_FALSE_POSITIVES = ("00075741", "387473359")

    def test_iso_dates_not_matched(self) -> None:
        for date in self.ISO_DATE_FALSE_POSITIVES:
            assert find_inline_phones(f"Last contact: {date} per CRM") == [], date

    def test_year_range_not_matched(self) -> None:
        assert find_inline_phones("Active 2019-2020 season") == []

    def test_bare_id_fragments_not_matched(self) -> None:
        for frag in self.ID_FRAGMENT_FALSE_POSITIVES:
            assert find_inline_phones(f"uid {frag} tail") == [], frag

    def test_crm_timeline_block_reports_no_phones(self) -> None:
        # The exact shape athenaeum#500 flags: a CRM Timeline of dates, zero phones.
        body = (
            "## CRM Timeline\n"
            "- First contact: 2015-12-03\n"
            "- Last CRM update: 2021-07-16\n"
            "- Last email: 2016-03-14\n"
        )
        assert find_inline_phones(body) == []

    def test_genuine_phones_still_matched(self) -> None:
        # The pre-existing true positives must be unaffected by the tightening.
        assert find_inline_phones("call +1-555-0100 now") == ["+1-555-0100"]
        assert find_inline_phones("(555) 010-0100") == ["(555) 010-0100"]
        # A bare, separator-free run in the E.164-plausible band is still a phone.
        assert find_inline_phones("cell 5551234567 anytime") == ["5551234567"]
        assert find_inline_phones("intl +447911123456 ok") == ["+447911123456"]


# ---------------------------------------------------------------------------
# Phone detector — a leading paren must NOT defeat the exclusions (issue athenaeum#683)
# ---------------------------------------------------------------------------
#
# athenaeum#500's date/id exclusions were anchored (`^\d`) / `isdigit()`-gated, so a
# single leading '(' — the dominant shape in the live corpus, where
# parenthesized dates in prose and parenthesized page-uid prefixes in
# `_index.md` produced 911 lint-pii findings across 107 files — slipped every
# excluded shape back through as a "phone", and `migrate-pii --all` would have
# rewritten 69 pages on those hits. `_PHONE_RE` folds an optional leading
# '[+(]' into its capture group, so the fix normalizes that delimiter before
# the exclusion checks; find_inline_phones and the egress scan_outbound_text
# share one definition (_is_excluded_phone_shape).


class TestPhoneDetectorParenthesized:
    @pytest.mark.parametrize(
        "text",
        [
            "2026-07-29",  # correct today
            "(2026-07-29)",  # WRONG before athenaeum#683
            "52785095",  # correct today
            "(52785095)",  # WRONG before athenaeum#683
            "2019-2020",  # correct today
            "(2019-2020)",  # WRONG before athenaeum#683
        ],
    )
    def test_reproduction_cases_report_no_phone(self, text: str) -> None:
        # The six find_inline_phones cases from athenaeum#683's Reproduction section.
        assert find_inline_phones(text) == [], text

    #: Excluded shapes taken VERBATIM from the live corpus (athenaeum#683's impact table
    #: and athenaeum#500's body) rather than retyped in canonical form — dates, year
    #: ranges, and bare uid/analytics id fragments. Each must stay a non-match
    #: regardless of the punctuation wrapped around it.
    EXCLUDED_SAMPLES = (
        "2026-07-29",  # athenaeum#683: parenthesized date in prose
        "2026-06-12",  # athenaeum#683: parenthesized date in prose
        "2015-12-03",  # athenaeum#500: CRM-timeline date
        "2026-04-16",  # athenaeum#500: frontmatter `updated:` date
        "2019-2020",  # athenaeum#683 / athenaeum#500: year range
        "52785095",  # athenaeum#683: `_index.md` page uid prefix
        "69541219",  # athenaeum#683: `_index.md` page uid prefix
        "00075741",  # athenaeum#500: page uid prefix
        "387473359",  # athenaeum#500: GA4 property id
    )

    @pytest.mark.parametrize("sample", EXCLUDED_SAMPLES)
    def test_exclusions_are_punctuation_invariant(self, sample: str) -> None:
        # An exclusion must not be defeated by surrounding punctuation — the
        # invariant behind BOTH athenaeum#500 and athenaeum#683. This kills the class, not just
        # the six literals above (Quine retro on athenaeum#683): it would have failed on
        # the day athenaeum#500 merged.
        base = find_inline_phones(sample)
        assert base == [], sample
        for wrapped in (f"({sample})", f"[{sample}]", f"{sample},", f"({sample}"):
            assert find_inline_phones(wrapped) == base, wrapped

    def test_genuine_phones_survive_surrounding_punctuation(self) -> None:
        # The normalization is for the exclusion CHECK only — a parenthesized
        # real phone is still matched and returned verbatim.
        assert find_inline_phones("call (555) 010-0100 today") == ["(555) 010-0100"]
        assert find_inline_phones("(+1-555-0100)") == ["+1-555-0100"]
        assert find_inline_phones("num 917-231-6130.") == ["917-231-6130"]


# ---------------------------------------------------------------------------
# Phone detector — the shapes athenaeum#683's paren fix did not reach (issue athenaeum#720)
# ---------------------------------------------------------------------------
#
# athenaeum#683 normalized a LEADING paren, cutting lint-pii from 911/107 to 456/274.
# The residual 456 was dominated by four shapes the paren fix did not cover
# (measured on the live corpus, develop @ 5513d80):
#   * issue-number lists joined by single or double hyphens
#   * dates in non-ISO orderings, and dotted dates
#   * a match that runs PAST a closing paren / across a newline into the next
#     number (the permissive `[\d\-.\s()]` class admits spaces and, via `\s`,
#     newlines)
# Each row of athenaeum#720's table is pinned below to the EXACT example value the
# issue cites, so a regression re-surfaces the specific corpus shape. The
# exclusion is normalization + structural classification (segment into digit
# groups + separator runs), not a literal blocklist — a new separator style
# needs no new rule, which this class also asserts.


class TestPhoneDetectorIssueNumberAndDateShapes:
    #: (label, example value taken verbatim from athenaeum#720's table, source page).
    #: Each value must classify as a non-phone regardless of surrounding prose.
    UNCOVERED_SHAPES = (
        ("double-dash issue-number list", "445--436--435--374"),
        ("single-dash issue-number list", "256-257-280"),
        ("parenthesized year range", "(2020--2021"),
        ("dotted / reordered date", "02-08-2018"),
        ("date bleeding into following text", "2026-04-27)\n\n1"),
        ("version-and-date", "1778 (2026-08-01"),
    )

    @pytest.mark.parametrize(
        "example",
        [pytest.param(v, id=label) for label, v in UNCOVERED_SHAPES],
    )
    def test_uncovered_shape_reports_no_phone(self, example: str) -> None:
        # Pinned to the literal example from the issue's measurement table.
        assert find_inline_phones(example) == [], example
        # …and still a non-match embedded in ordinary surrounding text.
        assert find_inline_phones(f"see {example} here") == [], example

    def test_issue_number_list_separators_are_generalized(self) -> None:
        # "a new separator style should not require a new rule" (AC4): the same
        # list classifies as non-phone whether joined by single hyphens, double
        # hyphens, dots, or spaces — none is a phone.
        for sep in ("-", "--", ".", " ", " - "):
            token = sep.join(("256", "257", "280"))
            assert find_inline_phones(f"refs {token} done") == [], token

    def test_non_iso_date_orderings_excluded(self) -> None:
        # Day-Month-Year, Month-Day-Year, and dotted variants — order-agnostic.
        for date in ("02-08-2018", "08-02-2018", "2018.08.02", "27.04.2026", "2018/08/02"):
            assert find_inline_phones(f"met on {date} again") == [], date

    def test_date_bleeding_across_paren_or_newline_excluded(self) -> None:
        # The permissive class let a date run past `)` / across a blank line
        # into the following number. Both are capture artifacts, not phones.
        assert find_inline_phones("closed 2026-04-27)\n\n1 item done") == []
        assert find_inline_phones("Issue 1778 (2026-08-01 shipped") == []

    def test_genuine_phones_unaffected_by_720(self) -> None:
        # The athenaeum#683/#500 true positives must survive the athenaeum#720 tightening.
        assert find_inline_phones("call +1-555-0100 now") == ["+1-555-0100"]
        assert find_inline_phones("(555) 010-0100") == ["(555) 010-0100"]
        assert find_inline_phones("cell 5551234567 anytime") == ["5551234567"]
        assert find_inline_phones("intl +447911123456 ok") == ["+447911123456"]
        assert find_inline_phones("num 917-231-6130 today") == ["917-231-6130"]

    def test_email_axis_is_untouched(self) -> None:
        # AC3: no email-axis change — the widened phone exclusions must not
        # alter which email-shaped tokens are found. Pinned with a count
        # assertion over a body carrying both a real email and every athenaeum#720
        # false-positive phone shape.
        body = (
            "Contact alice@example.com about issues 445--436--435--374 and\n"
            "256-257-280, dated 02-08-2018 and (2020--2021), ref 1778 (2026-08-01,\n"
            "closed 2026-04-27)\n\n1. Cc bob+tag@sub.example.co.uk please."
        )
        assert find_inline_emails(body) == ["alice@example.com", "bob+tag@sub.example.co.uk"]
        # And the phone axis on that same body is empty — all shapes excluded.
        assert find_inline_phones(body) == []

    def test_corpus_scan_reduction_mechanism(self, tmp_path: Path) -> None:
        # The reduction lint-pii sees on the live corpus, in miniature: a page
        # full of athenaeum#720 false-positive shapes yields ZERO findings, while a page
        # with a genuine phone still yields one. This is the deterministic
        # mechanism behind the 456 -> tens drop the operator confirms live.
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        (wiki / "false_positives.md").write_text(
            "## Refs\n445--436--435--374\n256-257-280\n"
            "## Dates\n02-08-2018\n2026-04-27)\n\n1\n(2020--2021)\n1778 (2026-08-01)\n",
            encoding="utf-8",
        )
        (wiki / "real_phone.md").write_text("Reach Alice at +1-555-0100.\n", encoding="utf-8")
        findings = scan_corpus_pii(wiki)
        # Only the genuine-phone page is a finding; the false-positive page is clean.
        assert [f.path.name for f in findings] == ["real_phone.md"]
        assert findings[0].phones == ["+1-555-0100"]


# ---------------------------------------------------------------------------
# Phone detector — labeled identifiers + the shapes athenaeum#720 partially
# covered (issue athenaeum#732)
# ---------------------------------------------------------------------------
#
# After athenaeum#720 the live phone axis still reported 187 findings. The
# largest surviving class was NOT ambiguous — the values are typed by their own
# surrounding prose (`QBO realm 1008563730`, GA4 `stream 5139685489`,
# `ISBN 9798183760910`). A preceding-token exclusion retires that class with no
# model call; ISBN-13 additionally has a structural tell (13 digits, 978/979
# prefix) so an unlabeled ISBN is caught without the prose. Three smaller
# deterministic shapes survive alongside — the 4-group single-dash issue list
# (an off-by-one in the group-count bound), datetime-with-space, and four-part
# dates. Each value below is taken VERBATIM from athenaeum#732's measurement so a
# regression re-surfaces the exact corpus shape.


class TestPhoneDetectorLabeledIdentifiers:
    #: (label, value) pairs quoted verbatim from athenaeum#732's Class-1 table,
    #: each shown in the surrounding prose the corpus actually carries.
    LABELED = (
        ("QBO realm", 'the business entity "Kromatic" (QBO realm 1008563730)'),
        ("GA4 stream (backtick-wrapped)", "prod GA4 stream (`G-EYDNWEV55B`, stream `5139685489`)"),
        ("ISBN paperback (backtick)", "**paperback `9798196294006`;"),
        ("ISBN hardcover (backtick)", "**hardcover `9798183760910`**"),
        ("ISBN label + space", "ISBN 9798183760910 in the colophon"),
        ("ISBN-13 hyphenated label", "ISBN-13 9798196355028 listed"),
        ("realm with colon", "billing realm: 1008563730 for the tenant"),
    )

    @pytest.mark.parametrize(
        "sample", [pytest.param(v, id=label) for label, v in LABELED]
    )
    def test_labeled_identifier_is_not_a_phone(self, sample: str) -> None:
        assert find_inline_phones(sample) == [], sample

    def test_isbn13_is_excluded_structurally_without_a_label(self) -> None:
        # AC2: the 978/979 Bookland prefix at 13 digits is caught structurally,
        # so an unlabeled ISBN needs no adjacent prose. All four cited values.
        for isbn in ("9798183760910", "9798196294006", "9798196355028", "9781700393777"):
            assert find_inline_phones(f"see {isbn} elsewhere") == [], isbn

    def test_a_new_label_is_a_data_entry_not_a_code_path(self) -> None:
        # AC1: exclusions are keyed off a DATA list of labels — the same code
        # path retires every label. Assert the shipped label set explicitly so
        # dropping one is a visible diff, not a silent regression.
        from athenaeum.pii import LABELED_IDENTIFIER_PREFIXES

        assert {"qbo realm", "realm", "stream", "isbn"} <= set(LABELED_IDENTIFIER_PREFIXES)

    def test_label_does_not_eat_an_unrelated_following_phone(self) -> None:
        # The prefix must sit IMMEDIATELY before the run. A genuine phone that
        # merely follows a label word (with other tokens between) stays flagged.
        assert find_inline_phones("realm42 917-231-6130") == ["917-231-6130"]
        assert find_inline_phones("the stream ended; call 917-231-6130") == ["917-231-6130"]


class TestPhoneDetectorResidualShapes732:
    #: The 4-group single-dash issue lists athenaeum#720's group-count bound let
    #: through (verbatim from the issue), plus datetime-with-space and four-part
    #: dates. Every one must classify as a non-phone.
    RESIDUAL_SHAPES = (
        ("4-group single-dash list", "410-414-416-412"),
        ("4-group single-dash list", "790-791-792-793"),
        ("4-group single-dash list", "743-721-714-695"),
        ("4-group single-dash list", "109-110-111-112"),
        ("4-group single-dash list", "245-338-339-352"),
        ("4-group single-dash list", "801-835-841-843"),
        ("datetime with space", "2026-04-23 05"),
        ("datetime with space", "2026-05-31 08"),
        ("datetime with space", "2026-04-24 11"),
        ("four-part date", "2018-05-06-07"),
    )

    @pytest.mark.parametrize(
        "example", [pytest.param(v, id=f"{label}:{v}") for label, v in RESIDUAL_SHAPES]
    )
    def test_residual_shape_reports_no_phone(self, example: str) -> None:
        assert find_inline_phones(example) == [], example
        assert find_inline_phones(f"ref {example} end") == [], example

    def test_group_count_bound_has_no_upper_limit(self) -> None:
        # AC3: the bound is a lower bound (>=4 groups, no '+'), so a 5- or
        # 6-group list does not reopen the class.
        assert find_inline_phones("410-414-416-412-419") == []
        assert find_inline_phones("410-414-416-412-419-421") == []


class TestPhoneDetector732GenuineNumbersStayFlagged:
    def test_the_two_pinned_genuine_numbers_remain_flagged(self) -> None:
        # AC5 — the criterion that keeps the fix honest. These real numbers must
        # never be retired by any athenaeum#732 rule.
        assert find_inline_phones("call 917-231-6130 today") == ["917-231-6130"]
        assert find_inline_phones("reach us at 206-330-3783 please") == ["206-330-3783"]

    def test_prior_genuine_survivors_unaffected(self) -> None:
        # The athenaeum#500/#683/#720 true positives survive the athenaeum#732 tightening.
        assert find_inline_phones("call +1-555-0100 now") == ["+1-555-0100"]
        assert find_inline_phones("(555) 010-0100") == ["(555) 010-0100"]
        assert find_inline_phones("cell 5551234567 anytime") == ["5551234567"]
        assert find_inline_phones("intl +447911123456 ok") == ["+447911123456"]

    def test_email_axis_is_untouched(self) -> None:
        # AC7: NO email-axis change — pinned with a count assertion over a body
        # carrying a real email and every athenaeum#732 false-positive shape.
        body = (
            "Contact alice@example.com re QBO realm 1008563730 and GA4 stream\n"
            "`5139685489`, ISBN 9798183760910, issues 410-414-416-412, at\n"
            "2026-04-23 05 on 2018-05-06-07. Cc bob+tag@sub.example.co.uk please."
        )
        assert find_inline_emails(body) == ["alice@example.com", "bob+tag@sub.example.co.uk"]
        # And the phone axis on that same body is empty — all shapes excluded.
        assert find_inline_phones(body) == []

    def test_corpus_scan_retires_labeled_page_keeps_genuine(self, tmp_path: Path) -> None:
        # The live reduction in miniature: a page full of athenaeum#732 labeled
        # identifiers + residual shapes yields ZERO findings, while a page with a
        # genuine phone still yields one.
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        (wiki / "labeled_ids.md").write_text(
            "## Ledger\nQBO realm 1008563730\nGA4 stream `5139685489`\n"
            "ISBN 9798183760910\n## Refs\n410-414-416-412\n2018-05-06-07\n",
            encoding="utf-8",
        )
        (wiki / "real_phone.md").write_text("Reach Alice at 917-231-6130.\n", encoding="utf-8")
        findings = scan_corpus_pii(wiki)
        assert [f.path.name for f in findings] == ["real_phone.md"]
        assert findings[0].phones == ["917-231-6130"]


# ---------------------------------------------------------------------------
# Observation log — append, read, supersession, deterministic fold
# ---------------------------------------------------------------------------


class TestObservationLog:
    def test_append_and_read_roundtrip(self, tmp_path: Path) -> None:
        root = tmp_path / "contacts"
        obs = append_observation(
            root,
            obs_id="obs-1",
            identifier="alice@example.com",
            person_id="person-alice",
            observed_at="2026-01-01T00:00:00Z",
            source_msg_id="msg-1",
        )
        assert obs == Observation(
            obs_id="obs-1",
            identifier="alice@example.com",
            person_id="person-alice",
            observed_at="2026-01-01T00:00:00Z",
            source_msg_id="msg-1",
        )
        read = read_observations(root)
        assert read == [obs]

    def test_read_missing_log_returns_empty(self, tmp_path: Path) -> None:
        assert read_observations(tmp_path / "nope") == []
        assert read_supersessions(tmp_path / "nope") == []

    def test_append_only_multiple_lines(self, tmp_path: Path) -> None:
        root = tmp_path / "contacts"
        append_observation(
            root,
            obs_id="obs-1",
            identifier="a@example.com",
            person_id="p1",
            observed_at="2026-01-01T00:00:00Z",
            source_msg_id="m1",
        )
        append_observation(
            root,
            obs_id="obs-2",
            identifier="b@example.com",
            person_id="p2",
            observed_at="2026-01-02T00:00:00Z",
            source_msg_id="m2",
        )
        recs = read_observations(root)
        assert [r.obs_id for r in recs] == ["obs-1", "obs-2"]

    def test_tolerant_reader_skips_torn_trailing_line(self, tmp_path: Path) -> None:
        root = tmp_path / "contacts"
        append_observation(
            root,
            obs_id="obs-1",
            identifier="a@example.com",
            person_id="p1",
            observed_at="2026-01-01T00:00:00Z",
            source_msg_id="m1",
        )
        from athenaeum.pii import default_observation_log_path

        path = default_observation_log_path(root)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write('{"obs_id": "obs-2", "identifier": "b@ex')  # torn, no newline
        recs = read_observations(root)
        assert [r.obs_id for r in recs] == ["obs-1"]

    def test_supersession_append_and_read(self, tmp_path: Path) -> None:
        root = tmp_path / "contacts"
        sup = append_supersession(root, retracts="obs-1", reason="reassigned inbox")
        assert sup.retracts == "obs-1"
        assert sup.reason == "reassigned inbox"
        assert sup.at  # timestamp auto-stamped
        recs = read_supersessions(root)
        assert recs == [sup]

    def test_supersession_explicit_at(self, tmp_path: Path) -> None:
        root = tmp_path / "contacts"
        sup = append_supersession(
            root, retracts="obs-1", reason="typo", at="2026-02-01T00:00:00Z"
        )
        assert sup.at == "2026-02-01T00:00:00Z"


class TestFoldObservations:
    def test_simple_fold_no_supersession(self) -> None:
        obs = [
            Observation("o1", "alice@example.com", "p-alice", "2026-01-01T00:00:00Z", "m1"),
        ]
        folded = fold_observations(obs)
        assert folded == {"alice@example.com": [obs[0]]}

    def test_supersession_retracts_observation(self) -> None:
        obs = [
            Observation("o1", "alice@example.com", "p-alice", "2026-01-01T00:00:00Z", "m1"),
        ]
        sups = [Supersession(retracts="o1", reason="bad data", at="2026-01-02T00:00:00Z")]
        assert fold_observations(obs, sups) == {}

    def test_shared_address_returns_all_persons(self) -> None:
        # A genuinely shared address: two DIFFERENT persons both attributed —
        # both must survive the fold (not just the latest write).
        obs = [
            Observation("o1", "team@example.com", "p-alice", "2026-01-01T00:00:00Z", "m1"),
            Observation("o2", "team@example.com", "p-bob", "2026-01-02T00:00:00Z", "m2"),
        ]
        folded = fold_observations(obs)
        assert {o.person_id for o in folded["team@example.com"]} == {"p-alice", "p-bob"}

    def test_jason_janice_correction_resolves_latest_uncontradicted(self) -> None:
        # identifier first attributed to Jason, later corrected to Janice.
        # A taken-over-inbox re-observation to a DIFFERENT person_id under the
        # SAME identifier is itself just a new observation (identifier->person
        # is ~1:1 in spirit, but the fold does not forbid a second write —
        # the correction is expressed as an explicit supersession retracting
        # the original wrong attribution, so the fold never "guesses").
        obs = [
            Observation(
                "o-jason", "jt@example.com", "p-jason", "2026-01-01T00:00:00Z", "m1"
            ),
        ]
        sups = [
            Supersession(
                retracts="o-jason",
                reason="jt@example.com actually belongs to Janice, not Jason",
                at="2026-03-01T00:00:00Z",
            )
        ]
        # After the correction, a fresh observation attributes the identifier
        # to Janice.
        obs.append(
            Observation(
                "o-janice", "jt@example.com", "p-janice", "2026-03-01T00:05:00Z", "m2"
            )
        )
        folded = fold_observations(obs, sups)
        live = folded["jt@example.com"]
        assert [o.person_id for o in live] == ["p-janice"]

    def test_latest_per_person_survives_deterministically(self) -> None:
        # Two observations for the SAME identifier + SAME person_id (a
        # re-confirmation) — only the latest (by observed_at) survives.
        obs = [
            Observation("o1", "alice@example.com", "p-alice", "2026-01-01T00:00:00Z", "m1"),
            Observation("o2", "alice@example.com", "p-alice", "2026-02-01T00:00:00Z", "m2"),
        ]
        folded = fold_observations(obs)
        assert len(folded["alice@example.com"]) == 1
        assert folded["alice@example.com"][0].obs_id == "o2"

    def test_tie_break_is_deterministic_on_obs_id(self) -> None:
        # Same observed_at timestamp, same person_id — tie-break must be
        # stable regardless of input order.
        a = Observation("o-a", "alice@example.com", "p-alice", "2026-01-01T00:00:00Z", "m1")
        b = Observation("o-b", "alice@example.com", "p-alice", "2026-01-01T00:00:00Z", "m2")
        folded_1 = fold_observations([a, b])
        folded_2 = fold_observations([b, a])
        assert folded_1 == folded_2
        assert folded_1["alice@example.com"][0].obs_id == "o-b"  # "o-b" > "o-a" lexically

    def test_no_clustering_distinct_person_ids_never_merged(self) -> None:
        # Two different person_ids must never collapse into one entry even
        # when their content/identifier is otherwise identical — the fold is
        # a deterministic string-equality operation, not a similarity merge.
        obs = [
            Observation("o1", "x@example.com", "p-1", "2026-01-01T00:00:00Z", "m1"),
            Observation("o2", "x@example.com", "p-2", "2026-01-01T00:00:00Z", "m1"),
        ]
        folded = fold_observations(obs)
        assert len(folded["x@example.com"]) == 2

    def test_resolve_identifier_convenience(self) -> None:
        obs = [
            Observation("o1", "team@example.com", "p-alice", "2026-01-01T00:00:00Z", "m1"),
            Observation("o2", "team@example.com", "p-bob", "2026-01-02T00:00:00Z", "m2"),
        ]
        result = resolve_identifier("team@example.com", obs)
        assert {o.person_id for o in result} == {"p-alice", "p-bob"}
        assert resolve_identifier("unknown@example.com", obs) == []

    def test_unretracted_supersession_target_is_a_noop(self) -> None:
        # A supersession retracting an obs_id that was never observed (or
        # already pruned) must not raise — it just has nothing to retract.
        obs = [
            Observation("o1", "alice@example.com", "p-alice", "2026-01-01T00:00:00Z", "m1"),
        ]
        sups = [Supersession(retracts="does-not-exist", reason="n/a", at="2026-01-01T00:00:00Z")]
        folded = fold_observations(obs, sups)
        assert folded["alice@example.com"][0].obs_id == "o1"


# ---------------------------------------------------------------------------
# Integration: observation log lives on the (excluded) contacts surface
# ---------------------------------------------------------------------------


class TestObservationLogOnExcludedSurface:
    def test_log_written_under_resolved_contacts_root(self, tmp_path: Path) -> None:
        knowledge_root = tmp_path / "knowledge"
        contacts_root = contacts_surface_root(knowledge_root, EXCLUDED_CONFIG)
        append_observation(
            contacts_root,
            obs_id="o1",
            identifier="alice@example.com",
            person_id="p-alice",
            observed_at="2026-01-01T00:00:00Z",
            source_msg_id="m1",
        )
        from athenaeum.pii import OBSERVATION_LOG_FILENAME

        log_path = contacts_root / OBSERVATION_LOG_FILENAME
        assert log_path.exists()
        # And the log itself lives outside wiki/, so it is never scanned by
        # the corpus builders either (same by-construction guarantee).
        assert (knowledge_root / "wiki") not in log_path.parents

    def test_surface_root_for_class_matches_contacts_surface_root(
        self, tmp_path: Path
    ) -> None:
        knowledge_root = tmp_path / "knowledge"
        assert contacts_surface_root(knowledge_root, EXCLUDED_CONFIG) == surface_root_for_class(
            "pii", EXCLUDED_CONFIG, knowledge_root
        )


# ---------------------------------------------------------------------------
# Hard-bounce recognition + mark (issue athenaeum#765)
# ---------------------------------------------------------------------------


class TestDetectHardBounceFact:
    def test_recognizes_hard_bounce(self) -> None:
        fact = detect_hard_bounce_fact(
            "Alex's address alex@example.org hard-bounced. "
            "Diagnostic: 550 5.1.1 user unknown."
        )
        assert fact == HardBounceFact(
            identifier="alex@example.org",
            diagnostic=(
                "Alex's address alex@example.org hard-bounced. "
                "Diagnostic: 550 5.1.1 user unknown."
            ),
        )

    def test_declines_transient_4xx_code(self) -> None:
        # voltaire#81's "potentially stale" case — out of scope for this
        # issue. A 4.x code must never be recognized as a hard bounce.
        assert (
            detect_hard_bounce_fact(
                "alex@example.org soft-bounced. Diagnostic: 421 4.4.62 "
                "routing issue."
            )
            is None
        )

    def test_declines_no_diagnostic(self) -> None:
        assert detect_hard_bounce_fact("alex@example.org seems unreachable.") is None

    def test_declines_ambiguous_multiple_addresses(self) -> None:
        assert (
            detect_hard_bounce_fact(
                "alex@example.org and blair@example.org both bounced: 550 5.1.1"
            )
            is None
        )

    def test_declines_no_address(self) -> None:
        assert detect_hard_bounce_fact("Something bounced: 550 5.1.1 user unknown.") is None

    def test_declines_empty_text(self) -> None:
        assert detect_hard_bounce_fact("") is None

    def test_bare_status_code_still_recognized(self) -> None:
        # The diagnostic need not carry the leading SMTP reply code.
        fact = detect_hard_bounce_fact("blair@example.org: 5.1.1")
        assert fact is not None
        assert fact.identifier == "blair@example.org"


class TestMarkBounced:
    def test_creates_record_carrying_diagnostic_observed_at_source(
        self, tmp_path: Path
    ) -> None:
        contacts_root = tmp_path / "contacts"
        path, changed = mark_bounced(
            contacts_root,
            "alex@example.org",
            diagnostic="550 5.1.1 user unknown",
            observed_at="2026-08-05",
            source="script:voltaire-bounce-relay",
        )
        assert changed is True
        assert path.exists()
        meta = read_bounce_record(path)
        assert meta["identifier"] == "alex@example.org"
        assert meta["bounce_diagnostic"] == "550 5.1.1 user unknown"
        assert meta["observed_at"] == "2026-08-05"
        assert meta["source"] == "script:voltaire-bounce-relay"
        assert meta["pii"] is True

    def test_default_path_is_under_contacts_root(self, tmp_path: Path) -> None:
        contacts_root = tmp_path / "contacts"
        path, _ = mark_bounced(
            contacts_root,
            "alex@example.org",
            diagnostic="550 5.1.1",
            observed_at="2026-08-05",
            source="manual",
        )
        assert path == default_bounce_record_path(contacts_root, "alex@example.org")
        assert path.parent == contacts_root

    def test_encoded_as_valid_time_close_not_a_status_enum(self, tmp_path: Path) -> None:
        # The mark is a valid-time close (athenaeum#308's existing mechanism),
        # never a `bounced`/`deprecated` status field — athenaeum#765 explicitly
        # cuts that as a durable representation.
        contacts_root = tmp_path / "contacts"
        path, _ = mark_bounced(
            contacts_root,
            "alex@example.org",
            diagnostic="550 5.1.1",
            observed_at="2026-08-05",
            source="manual",
        )
        meta = read_bounce_record(path)
        assert meta["valid_until"] == "2026-08-05"
        assert "status" not in meta
        assert "bounced" not in meta
        assert "deprecated" not in meta

    def test_idempotent_reporting_same_bounce_is_a_noop(self, tmp_path: Path) -> None:
        contacts_root = tmp_path / "contacts"
        path1, changed1 = mark_bounced(
            contacts_root,
            "alex@example.org",
            diagnostic="550 5.1.1 user unknown",
            observed_at="2026-08-05",
            source="script:voltaire-bounce-relay",
        )
        before = path1.read_text(encoding="utf-8")
        path2, changed2 = mark_bounced(
            contacts_root,
            "alex@example.org",
            diagnostic="550 5.1.1 user unknown",
            observed_at="2026-08-05",
            source="script:voltaire-bounce-relay",
        )
        assert changed1 is True
        assert changed2 is False  # re-reporting the identical fact: no-op
        assert path1 == path2
        assert path2.read_text(encoding="utf-8") == before  # byte-for-byte stable

    def test_re_bounce_updates_in_place_no_duplicate_record(self, tmp_path: Path) -> None:
        contacts_root = tmp_path / "contacts"
        mark_bounced(
            contacts_root,
            "alex@example.org",
            diagnostic="550 5.1.1 user unknown",
            observed_at="2026-08-05",
            source="script:first-report",
        )
        path, changed = mark_bounced(
            contacts_root,
            "alex@example.org",
            diagnostic="550 5.1.1 mailbox disabled",
            observed_at="2026-08-20",
            source="script:second-report",
        )
        assert changed is True
        meta = read_bounce_record(path)
        assert meta["observed_at"] == "2026-08-20"
        assert meta["valid_until"] == "2026-08-20"
        assert meta["bounce_diagnostic"] == "550 5.1.1 mailbox disabled"
        # Still exactly one record for this identifier — updated, not duplicated.
        assert list(contacts_root.glob("*.md")) == [path]

    def test_preserves_existing_frontmatter_fields(self, tmp_path: Path) -> None:
        contacts_root = tmp_path / "contacts"
        contacts_root.mkdir(parents=True)
        record_path = default_bounce_record_path(contacts_root, "alex@example.org")
        record_path.write_text(
            "---\nidentifier: alex@example.org\nname: Alex\n---\n\nBody.\n",
            encoding="utf-8",
        )
        _, changed = mark_bounced(
            contacts_root,
            "alex@example.org",
            diagnostic="550 5.1.1",
            observed_at="2026-08-05",
            source="manual",
        )
        assert changed is True
        meta = read_bounce_record(record_path)
        assert meta["name"] == "Alex"  # untouched pre-existing field survives

    def test_nothing_deleted_identifier_stays_on_disk(self, tmp_path: Path) -> None:
        contacts_root = tmp_path / "contacts"
        path, _ = mark_bounced(
            contacts_root,
            "alex@example.org",
            diagnostic="550 5.1.1",
            observed_at="2026-08-05",
            source="manual",
        )
        assert path.exists()
        meta = read_bounce_record(path)
        assert meta["identifier"] == "alex@example.org"

    def test_no_new_ledger_file_created(self, tmp_path: Path) -> None:
        contacts_root = tmp_path / "contacts"
        mark_bounced(
            contacts_root,
            "alex@example.org",
            diagnostic="550 5.1.1",
            observed_at="2026-08-05",
            source="manual",
        )
        assert list(contacts_root.glob("*.jsonl")) == []


class TestIsBounced:
    def test_read_back_true_after_valid_until_passes(self, tmp_path: Path) -> None:
        from datetime import date

        contacts_root = tmp_path / "contacts"
        path, _ = mark_bounced(
            contacts_root,
            "alex@example.org",
            diagnostic="550 5.1.1",
            observed_at="2026-08-05",
            source="manual",
        )
        meta = read_bounce_record(path)
        assert is_bounced(meta, as_of=date(2026, 8, 6)) is True
        # still deliverable through this date
        assert is_bounced(meta, as_of=date(2026, 8, 5)) is False

    def test_absent_record_reads_as_not_bounced(self) -> None:
        assert is_bounced({}) is False
        assert is_bounced(None) is False

    def test_present_but_non_deliverable_distinguishable_from_absent(
        self, tmp_path: Path
    ) -> None:
        from datetime import date

        contacts_root = tmp_path / "contacts"
        path, _ = mark_bounced(
            contacts_root,
            "alex@example.org",
            diagnostic="550 5.1.1",
            observed_at="2026-08-05",
            source="manual",
        )
        meta = read_bounce_record(path)
        # Present (readable identifier + diagnostic) AND flagged non-deliverable —
        # never absent, per the issue's AC.
        assert meta["identifier"] == "alex@example.org"
        assert is_bounced(meta, as_of=date(2026, 9, 1)) is True
