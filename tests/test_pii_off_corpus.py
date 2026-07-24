# SPDX-License-Identifier: Apache-2.0
"""Tests for PII off-corpus (issue #427): excluded contacts surface, entity-page
lint, and the append-only observation log + supersession fold.

Structure mirrors the issue's acceptance criteria:

- ``TestExcludedSurfaceFailsClosed`` — a page on the contacts (excluded)
  surface never appears in embeddings (vector), FTS5 recall, keyword recall,
  or merge proposals. One test per consumer, proving the exclusion is
  inherited BY CONSTRUCTION through #429's adapter interface (fail-closed) —
  no #427-specific code path in the consumer, just the adapter's excluded
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
    Observation,
    Supersession,
    append_observation,
    append_supersession,
    contacts_surface_root,
    find_inline_emails,
    find_inline_phones,
    fold_observations,
    has_inline_contact_fields,
    is_pii_class_excluded,
    is_pii_flagged,
    lint_inline_contact_fields,
    read_observations,
    read_supersessions,
    resolve_identifier,
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
        # ordinary wiki surface, matching #429's "unconfigured = default" rule.
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
        # glob candidate — by construction, not a #427-specific filter.
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
        assert find_inline_phones("issue #427 page 12") == []

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
