# SPDX-License-Identifier: Apache-2.0
"""Tests for the batch person read (issue athenaeum#877).

``read_person`` rebuilds two O(corpus) indexes per call — the wiki
``EntityIndex`` and a full ``iter_contact_records()`` scan — so N uids cost N
full passes over the store (~28s per uid on the live 16,928-page corpus, ~37
hours for the 4,696-person population ``apollo-enrich``'s weekly job
resolves). ``read_people`` pays each scan ONCE per batch instead.

The fix is a cost change, not a semantic one, so the tests come in two halves:

- ``TestScanCostIsPaidOnce`` — the issue's AC #1, asserted directly by
  COUNTING index builds across a multi-uid batch. This is the test that would
  fail if either half of the fix regressed, including the ``EntityIndex``
  half the issue text does not name but which is the larger share of the
  measured 28s.
- ``TestBatchMatchesSingleRead`` — parity across all four cells of
  ``{include on, include off} x {record present, absent}``, plus unknown
  uids, class filtering and the redaction markers. ``read_people`` must
  return exactly what ``read_person`` returns, or the speedup is a
  behavior change wearing a performance change's clothes.

Plus ``TestBuildContactRecordUidIndex`` (the shared-uid determinism the
single-lookup resolver already promises, held to the same discipline in
batch), ``TestStreamOrderAndLaziness`` (input order, duplicates, lazy reads)
and ``TestCallerNeverConstructsSurfacePath`` (the two-path invariant,
``docs/one-way-in-one-way-out.md`` §3, which a batch entry point had to
preserve to be the right fix at all).

Fixtures follow ``tests/test_person_read.py``'s ``EXCLUDED_CONFIG`` +
tmp-path builders rather than inventing new ones. Ordinary
``alex@example.org``-style addresses only, matching that file.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from athenaeum import pii
from athenaeum.pii import (
    USAGE_CLASS_OBSERVED,
    USAGE_CLASS_PROVIDER,
    PersonRead,
    RedactionMarker,
    build_contact_record_uid_index,
    classify_contact_value,
    contacts_surface_root,
    read_people,
    read_person,
)

# Issue athenaeum#887: this module exercises the DEPRECATED person-shaped entry
# points on purpose — they are the behaviour/parity tests that must keep
# passing unchanged until athenaeum#888 actually removes them, which is exactly
# what "deprecated, not changed" means. The specific warning is filtered here
# so the suite stays readable; it is NOT filtered globally, and that it fires
# at all (with the right message, at the caller's line, at CALL time for the
# lazy batch form) is asserted directly in tests/test_read_person_deprecation.py.
pytestmark = pytest.mark.filterwarnings("ignore:pii.read_p:DeprecationWarning")

EXCLUDED_CONFIG = {"storage": {"mapping": {"pii": "excluded"}}}


def _write_wiki_person(wiki_root: Path, uid: str, *, name: str = "Alex Example") -> Path:
    """Minimal ``type: person`` wiki page indexable by ``EntityIndex``."""
    wiki_root.mkdir(parents=True, exist_ok=True)
    path = wiki_root / f"{uid}.md"
    path.write_text(
        f"---\nuid: {uid}\nname: {name}\ntype: person\n---\n\nNotes about {name}.\n",
        encoding="utf-8",
    )
    return path


def _write_contact_record(
    contacts_root: Path, filename: str, *, uid: str, fields: str = ""
) -> Path:
    """Synthetic contact record on the (excluded) contacts surface."""
    contacts_root.mkdir(parents=True, exist_ok=True)
    path = contacts_root / filename
    path.write_text(
        f"---\nuid: {uid}\npii: true\n{fields}---\n\nArchival contact data.\n",
        encoding="utf-8",
    )
    return path


def _corpus(tmp_path: Path, uids: list[str], *, with_records: bool = True) -> Path:
    """A knowledge root with a wiki page (and optionally a record) per uid."""
    knowledge = tmp_path / "knowledge"
    contacts_root = contacts_surface_root(knowledge, EXCLUDED_CONFIG)
    for uid in uids:
        _write_wiki_person(knowledge / "wiki", uid, name=f"Person {uid}")
        if with_records:
            _write_contact_record(
                contacts_root,
                f"{uid}-contact.md",
                uid=uid,
                fields=f"emails:\n  - {uid}@example.org\n",
            )
    return knowledge


class _CountingCalls:
    """Count calls to a patched module-level function, delegating to the real one."""

    def __init__(self, real: Callable[..., Any]) -> None:
        self._real = real
        self.calls = 0

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.calls += 1
        return self._real(*args, **kwargs)


@pytest.fixture
def index_counters(monkeypatch: pytest.MonkeyPatch) -> tuple[_CountingCalls, _CountingCalls]:
    """Counters for the two O(corpus) index builds, patched into ``athenaeum.pii``.

    Both are looked up from the module's globals at call time, so patching
    the names on the module is enough to observe every build either entry
    point performs.
    """
    scans = _CountingCalls(pii.iter_contact_records)
    entity_indexes = _CountingCalls(pii.EntityIndex)
    monkeypatch.setattr(pii, "iter_contact_records", scans)
    monkeypatch.setattr(pii, "EntityIndex", entity_indexes)
    return scans, entity_indexes


# ---------------------------------------------------------------------------
# AC #1 — the O(corpus) scan is paid once per batch, not once per uid
# ---------------------------------------------------------------------------


class TestScanCostIsPaidOnce:
    def test_contacts_scan_runs_once_for_many_uids(
        self, tmp_path: Path, index_counters: tuple[_CountingCalls, _CountingCalls]
    ) -> None:
        """AC: N uids in one process pay the ``iter_contact_records`` scan O(1) times."""
        scans, _ = index_counters
        uids = [f"person-{n}" for n in range(12)]
        knowledge = _corpus(tmp_path, uids)

        results = list(read_people(knowledge, EXCLUDED_CONFIG, uids))

        assert len(results) == 12
        assert scans.calls == 1

    def test_entity_index_is_built_once_for_many_uids(
        self, tmp_path: Path, index_counters: tuple[_CountingCalls, _CountingCalls]
    ) -> None:
        """The other O(corpus) half: the wiki index is built once, not per uid.

        Not named in the issue text, but it is the LARGER share of the
        measured 28s/call (a bare frontmatter pass over the 16,928-page wiki
        measured 25.2s on its own). Fixing only the contacts scan would have
        left ~33 of the projected 37 hours in place, so this assertion is as
        load-bearing as the one above.
        """
        _, entity_indexes = index_counters
        uids = [f"person-{n}" for n in range(12)]
        knowledge = _corpus(tmp_path, uids)

        list(read_people(knowledge, EXCLUDED_CONFIG, uids))

        assert entity_indexes.calls == 1

    def test_cost_is_flat_as_the_batch_grows(
        self, tmp_path: Path, index_counters: tuple[_CountingCalls, _CountingCalls]
    ) -> None:
        """The property stated as a property: index builds do not scale with N.

        A per-uid implementation passes the two assertions above only by
        accident of N; this one pins the shape — 1 uid and 40 uids cost the
        same number of scans.
        """
        scans, entity_indexes = index_counters
        uids = [f"person-{n}" for n in range(40)]
        knowledge = _corpus(tmp_path, uids)

        list(read_people(knowledge, EXCLUDED_CONFIG, uids[:1]))
        after_one = (scans.calls, entity_indexes.calls)
        list(read_people(knowledge, EXCLUDED_CONFIG, uids))
        after_forty = (scans.calls, entity_indexes.calls)

        assert after_one == (1, 1)
        # One more batch ran, so exactly one more of each — not forty more.
        assert after_forty == (2, 2)

    def test_single_read_still_pays_per_call(
        self, tmp_path: Path, index_counters: tuple[_CountingCalls, _CountingCalls]
    ) -> None:
        """The baseline the fix exists to beat, kept honest.

        ``read_person`` is deliberately unchanged in cost: a single read has
        nothing to amortize against. This documents that the loop-shaped
        caller is the thing that must move to ``read_people`` — the speedup
        is not something ``read_person`` silently acquired.
        """
        scans, entity_indexes = index_counters
        uids = [f"person-{n}" for n in range(5)]
        knowledge = _corpus(tmp_path, uids)

        for uid in uids:
            read_person(knowledge, EXCLUDED_CONFIG, uid)

        assert scans.calls == 5
        assert entity_indexes.calls == 5


# ---------------------------------------------------------------------------
# Parity — a cost change, not a behavior change
# ---------------------------------------------------------------------------


class TestBatchMatchesSingleRead:
    @pytest.mark.parametrize("include_contact", [False, True])
    @pytest.mark.parametrize("with_records", [False, True])
    def test_four_cells_match_read_person_exactly(
        self, tmp_path: Path, include_contact: bool, with_records: bool
    ) -> None:
        """All four cells of {include on, off} x {record present, absent}."""
        uids = ["alex", "sam", "rowan"]
        knowledge = _corpus(tmp_path, uids, with_records=with_records)

        batch = dict(read_people(knowledge, EXCLUDED_CONFIG, uids, include_contact=include_contact))
        single = {
            uid: read_person(knowledge, EXCLUDED_CONFIG, uid, include_contact=include_contact)
            for uid in uids
        }

        assert batch == single
        # Not vacuous: every uid actually resolved to a page.
        assert all(isinstance(value, PersonRead) for value in batch.values())

    def test_redaction_markers_survive_the_batch(self, tmp_path: Path) -> None:
        """The withheld-vs-absent distinction is not lost by reading in bulk."""
        knowledge = _corpus(tmp_path, ["alex"], with_records=True)
        _write_wiki_person(knowledge / "wiki", "sam", name="Sam No Record")

        batch = dict(read_people(knowledge, EXCLUDED_CONFIG, ["alex", "sam"]))

        assert batch["alex"] is not None and batch["sam"] is not None
        assert batch["alex"].redactions == (RedactionMarker(field="emails", value_count=1),)
        assert batch["sam"].redactions == ()
        assert batch["alex"].contact == batch["sam"].contact == {}

    def test_unknown_uid_yields_none_without_ending_the_batch(self, tmp_path: Path) -> None:
        """A missing page is ``None`` for that uid only — the rest still read.

        The pair shape earns itself here: a bare sequence of reads would
        leave a caller unable to say WHICH uid the ``None`` belongs to.
        """
        knowledge = _corpus(tmp_path, ["alex", "rowan"])

        results = list(read_people(knowledge, EXCLUDED_CONFIG, ["alex", "no-such-uid", "rowan"]))

        assert [uid for uid, _ in results] == ["alex", "no-such-uid", "rowan"]
        assert results[1][1] is None
        assert results[0][1] is not None and results[2][1] is not None
        assert read_person(knowledge, EXCLUDED_CONFIG, "no-such-uid") is None

    def test_usage_class_filter_applies_to_every_result(self, tmp_path: Path) -> None:
        """``usage_classes`` is normalized once for the batch, applied per uid."""
        knowledge = tmp_path / "knowledge"
        contacts_root = contacts_surface_root(knowledge, EXCLUDED_CONFIG)
        for uid in ("alex", "sam"):
            _write_wiki_person(knowledge / "wiki", uid)
            _write_contact_record(
                contacts_root,
                f"{uid}-contact.md",
                uid=uid,
                fields=(f"emails:\n  - {uid}.seen@example.org\n  - {uid}.vendor@corp.example\n"),
            )
            classify_contact_value(
                contacts_root,
                f"{uid}.seen@example.org",
                usage_class=USAGE_CLASS_OBSERVED,
                source="agent-observed:inbox-sync",
                observed_at="2026-08-01T00:00:00Z",
            )
            classify_contact_value(
                contacts_root,
                f"{uid}.vendor@corp.example",
                usage_class=USAGE_CLASS_PROVIDER,
                source="provider:example",
                observed_at="2026-08-01T00:00:00Z",
            )

        batch = dict(
            read_people(
                knowledge,
                EXCLUDED_CONFIG,
                ["alex", "sam"],
                include_contact=True,
                usage_classes=[USAGE_CLASS_OBSERVED],
            )
        )

        for uid in ("alex", "sam"):
            result = batch[uid]
            assert result is not None
            assert result.contact["emails"] == [f"{uid}.seen@example.org"]
            assert [item.usage_class for item in result.classifications["emails"]] == [
                USAGE_CLASS_OBSERVED
            ]
            # Same call, one at a time, agrees.
            assert result == read_person(
                knowledge,
                EXCLUDED_CONFIG,
                uid,
                include_contact=True,
                usage_classes=[USAGE_CLASS_OBSERVED],
            )


# ---------------------------------------------------------------------------
# The uid -> record index
# ---------------------------------------------------------------------------


class TestBuildContactRecordUidIndex:
    def test_maps_every_uid_to_its_record(self, tmp_path: Path) -> None:
        knowledge = _corpus(tmp_path, ["alex", "sam"])
        contacts_root = contacts_surface_root(knowledge, EXCLUDED_CONFIG)

        index = build_contact_record_uid_index(contacts_root)

        assert set(index) == {"alex", "sam"}
        assert index["alex"].name == "alex-contact.md"

    def test_shared_uid_resolves_to_the_deterministic_first(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Same posture as ``resolve_contact_record_for_uid``: first wins, and warns.

        Load-bearing for the batch path: if the index resolved a shared uid
        differently from the single-lookup resolver, moving a caller to
        ``read_people`` would silently change WHICH record it read.
        """
        knowledge = tmp_path / "knowledge"
        contacts_root = contacts_surface_root(knowledge, EXCLUDED_CONFIG)
        _write_contact_record(contacts_root, "a-first.md", uid="shared")
        _write_contact_record(contacts_root, "z-second.md", uid="shared")

        with caplog.at_level(logging.WARNING, logger=pii.log.name):
            index = build_contact_record_uid_index(contacts_root)

        assert index["shared"].name == "a-first.md"
        assert index["shared"] == pii.resolve_contact_record_for_uid(contacts_root, "shared")
        assert "2 contact records" in caplog.text

    def test_empty_string_uid_is_never_indexed(self, tmp_path: Path) -> None:
        """An empty uid must match nothing — it would otherwise match every
        record with no ``uid:`` field, which is never the intent of a lookup."""
        knowledge = tmp_path / "knowledge"
        contacts_root = contacts_surface_root(knowledge, EXCLUDED_CONFIG)
        _write_contact_record(contacts_root, "empty.md", uid='""')
        _write_contact_record(contacts_root, "real.md", uid="alex")

        index = build_contact_record_uid_index(contacts_root)

        assert set(index) == {"alex"}

    def test_valueless_uid_matches_the_single_lookup_exactly(self, tmp_path: Path) -> None:
        """A valueless ``uid:`` is treated as absent by BOTH paths (athenaeum#878).

        ``uid:`` with no value parses to YAML null, and ``str(None)`` is the
        literal string ``"None"`` — before athenaeum#878, both
        ``resolve_contact_record_for_uid`` and this index independently
        stringified the raw value and so treated a valueless uid as if it
        were the uid ``"None"``. Fixed via a shared normalizer
        (``_normalize_frontmatter_uid``) that maps ``None`` to ``""``, the
        same sentinel an explicit ``uid: ""`` already normalizes to, so both
        paths now skip it identically. Kept as a parity test — not just
        "does neither path index it" but "do the two paths still agree" —
        because a batch read diverging from a single read on ANY uid, even
        a degenerate one, would defeat the point of athenaeum#877.
        """
        knowledge = tmp_path / "knowledge"
        contacts_root = contacts_surface_root(knowledge, EXCLUDED_CONFIG)
        _write_contact_record(contacts_root, "valueless.md", uid="")

        index = build_contact_record_uid_index(contacts_root)

        assert "None" not in index
        assert index.get("None") == pii.resolve_contact_record_for_uid(contacts_root, "None")

    def test_valueless_uid_never_matches_a_lookup_for_the_string_none(
        self, tmp_path: Path
    ) -> None:
        """A record with a valueless ``uid:`` must never resolve for uid ``"None"``.

        Distinct from the parity test above (which asserts the two paths
        AGREE): this asserts the shared, CORRECT behaviour directly — the
        literal string ``"None"`` must not resolve to a record whose uid
        merely happens to render that way via ``str(None)`` (athenaeum#878).
        """
        knowledge = tmp_path / "knowledge"
        contacts_root = contacts_surface_root(knowledge, EXCLUDED_CONFIG)
        _write_contact_record(contacts_root, "valueless.md", uid="")

        assert pii.resolve_contact_record_for_uid(contacts_root, "None") is None
        assert build_contact_record_uid_index(contacts_root).get("None") is None

    def test_missing_root_is_empty_not_an_error(self, tmp_path: Path) -> None:
        assert build_contact_record_uid_index(tmp_path / "nope") == {}

    def test_field_of_only_blank_values_is_neither_returned_nor_redacted(
        self, tmp_path: Path
    ) -> None:
        """A field present but empty has no value to withhold, so no marker.

        A redaction marker asserts a value EXISTS; emitting one for a field
        whose every value is blank would tell a caller to go looking for
        contact data that is not there.
        """
        knowledge = tmp_path / "knowledge"
        _write_wiki_person(knowledge / "wiki", "alex")
        contacts_root = contacts_surface_root(knowledge, EXCLUDED_CONFIG)
        _write_contact_record(
            contacts_root,
            "alex-contact.md",
            uid="alex",
            fields='emails:\n  - ""\n  - "   "\n',
        )

        redacted = dict(read_people(knowledge, EXCLUDED_CONFIG, ["alex"]))["alex"]
        included = dict(
            read_people(knowledge, EXCLUDED_CONFIG, ["alex"], include_contact=True)
        )["alex"]

        assert redacted is not None and included is not None
        assert redacted.redactions == ()
        assert included.contact == {}
        # And the single read agrees, as everywhere else.
        assert redacted == read_person(knowledge, EXCLUDED_CONFIG, "alex")

    def test_batch_read_over_missing_contacts_surface(self, tmp_path: Path) -> None:
        """No contacts surface at all: pages still read, no markers, no raise."""
        knowledge = _corpus(tmp_path, ["alex"], with_records=False)

        batch = dict(read_people(knowledge, EXCLUDED_CONFIG, ["alex"]))

        assert batch["alex"] is not None
        assert batch["alex"].contact_record_path is None
        assert batch["alex"].redactions == ()


# ---------------------------------------------------------------------------
# Stream shape
# ---------------------------------------------------------------------------


class TestStreamOrderAndLaziness:
    def test_input_order_is_preserved_and_duplicates_are_not_collapsed(
        self, tmp_path: Path
    ) -> None:
        """Why pairs-in-order rather than a dict: neither property survives one."""
        knowledge = _corpus(tmp_path, ["alex", "sam"])

        order = [uid for uid, _ in read_people(knowledge, EXCLUDED_CONFIG, ["sam", "alex", "sam"])]

        assert order == ["sam", "alex", "sam"]

    def test_empty_batch_reads_nothing(
        self, tmp_path: Path, index_counters: tuple[_CountingCalls, _CountingCalls]
    ) -> None:
        """No candidates → no scan at all, not one pass to read zero people.

        The indexes are built on the first uid rather than at call time
        precisely so a quiet week for the weekly enrichment job costs
        nothing instead of a full O(corpus) pass.
        """
        scans, entity_indexes = index_counters
        knowledge = _corpus(tmp_path, ["alex"])

        assert list(read_people(knowledge, EXCLUDED_CONFIG, [])) == []
        assert (scans.calls, entity_indexes.calls) == (0, 0)

    def test_nothing_is_read_until_the_first_pair_is_pulled(
        self, tmp_path: Path, index_counters: tuple[_CountingCalls, _CountingCalls]
    ) -> None:
        """The documented consequence of a lazy stream, pinned so it stays true.

        A caller holding thousands of ``PersonRead`` values at once would
        hold much of the corpus in memory (each carries a full page body);
        laziness is what keeps a 4,696-uid run flat.
        """
        scans, entity_indexes = index_counters
        knowledge = _corpus(tmp_path, ["alex", "sam"])

        stream = read_people(knowledge, EXCLUDED_CONFIG, ["alex", "sam"])
        assert (scans.calls, entity_indexes.calls) == (0, 0)

        next(stream)

        assert (scans.calls, entity_indexes.calls) == (1, 1)


# ---------------------------------------------------------------------------
# The two-path invariant (docs/one-way-in-one-way-out.md §3)
# ---------------------------------------------------------------------------


class TestCallerNeverConstructsSurfacePath:
    def test_batch_resolves_the_surface_itself(self, tmp_path: Path) -> None:
        """The caller supplies uids and flags — never a path.

        This is why a batch ENTRY POINT was the right fix rather than
        exporting the index for callers to scan themselves: the amortization
        happens on this side of the seam.
        """
        knowledge = _corpus(tmp_path, ["alex"])
        contacts_root = contacts_surface_root(knowledge, EXCLUDED_CONFIG)

        batch = dict(read_people(knowledge, EXCLUDED_CONFIG, ["alex"]))

        result = batch["alex"]
        assert result is not None
        assert result.contact_record_path is not None
        # Resolved UNDER the configured surface, which the test never passed in.
        assert result.contact_record_path.is_relative_to(contacts_root)
        assert not result.contact_record_path.is_relative_to(knowledge / "wiki")
