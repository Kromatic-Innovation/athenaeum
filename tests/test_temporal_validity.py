"""Tests for claim-level temporal validity (issue #308, slice 1).

Covers the ``valid_from:`` / ``valid_until:`` frontmatter fields and the
shared upper-bound predicate wired into BOTH inactive-memory checks:

- :func:`athenaeum.models.is_inactive_memory` (dict path, used by recall).
- :meth:`athenaeum.models.AutoMemoryFile.is_inactive` (dataclass path, used
  by the C3 merge compile).

Slice 1 is the frontmatter + read-filter foundation: the resolver does NOT
yet auto-stamp ``valid_until`` (slice 2) and there is no ``--as-of`` CLI view
(slice 3, for which the predicate already accepts an ``as_of`` parameter).

Determinism note: every temporal assertion passes an EXPLICIT ``as_of`` so a
claim's active/inactive verdict does not flip between today's test runs. One
test asserts the default really is :func:`date.today`.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from athenaeum.models import (
    AutoMemoryFile,
    is_inactive_memory,
    parse_valid_from,
    parse_valid_until,
    valid_until_expired,
    validity_bound_str,
    validity_windows_disjoint,
)
from athenaeum.search import FTS5Backend, KeywordBackend

PAST = date(2020, 1, 1)
FUTURE = date(2999, 12, 31)
ANCHOR = date(2026, 7, 5)  # explicit "today" for deterministic as_of tests


# ---------------------------------------------------------------------------
# Parsers — parse_valid_from / parse_valid_until
# ---------------------------------------------------------------------------


class TestParsers:
    def test_parses_iso_string(self) -> None:
        assert parse_valid_from({"valid_from": "2026-04-01"}) == date(2026, 4, 1)
        assert parse_valid_until({"valid_until": "2026-06-30"}) == date(2026, 6, 30)

    def test_parses_yaml_date_object(self) -> None:
        # YAML auto-parses a bare ``YYYY-MM-DD`` scalar into a datetime.date.
        assert parse_valid_until({"valid_until": date(2026, 6, 30)}) == date(
            2026, 6, 30
        )

    def test_datetime_reduced_to_date(self) -> None:
        # datetime subclasses date; slice 1 is date-resolution, so a time
        # component is dropped to a bare date (avoids date-vs-datetime compare).
        val = parse_valid_until({"valid_until": datetime(2026, 6, 30, 12, 0, 0)})
        assert val == date(2026, 6, 30)
        assert type(val) is date

    def test_absent_is_none(self) -> None:
        assert parse_valid_from(None) is None
        assert parse_valid_until({}) is None
        assert parse_valid_until({"valid_until": ""}) is None

    def test_malformed_is_none_fail_open(self) -> None:
        assert parse_valid_until({"valid_until": "not-a-date"}) is None
        assert parse_valid_until({"valid_until": "2026-13-99"}) is None
        assert parse_valid_until({"valid_until": [1, 2, 3]}) is None


# ---------------------------------------------------------------------------
# valid_until_expired — the shared upper-bound helper
# ---------------------------------------------------------------------------


class TestValidUntilExpired:
    def test_past_is_expired(self) -> None:
        assert valid_until_expired({"valid_until": PAST.isoformat()}, as_of=ANCHOR)

    def test_future_is_not_expired(self) -> None:
        assert not valid_until_expired(
            {"valid_until": FUTURE.isoformat()}, as_of=ANCHOR
        )

    def test_inclusive_last_valid_date(self) -> None:
        # valid_until is the LAST valid date (inclusive): on that day, active.
        assert not valid_until_expired(
            {"valid_until": ANCHOR.isoformat()}, as_of=ANCHOR
        )
        # The day after, inactive.
        assert valid_until_expired(
            {"valid_until": ANCHOR.isoformat()}, as_of=ANCHOR + timedelta(days=1)
        )

    def test_absent_upper_bound_never_expires(self) -> None:
        assert not valid_until_expired({}, as_of=ANCHOR)
        assert not valid_until_expired(None, as_of=ANCHOR)

    def test_malformed_never_expires_fail_open(self) -> None:
        assert not valid_until_expired({"valid_until": "garbage"}, as_of=ANCHOR)

    def test_default_as_of_is_today(self) -> None:
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        assert valid_until_expired({"valid_until": yesterday})
        assert not valid_until_expired({"valid_until": tomorrow})


# ---------------------------------------------------------------------------
# is_inactive_memory (dict path) — the recall predicate
# ---------------------------------------------------------------------------


class TestIsInactiveMemoryDict:
    def test_past_valid_until_is_inactive(self) -> None:
        assert is_inactive_memory({"valid_until": PAST.isoformat()}, as_of=ANCHOR)

    def test_future_valid_until_is_active(self) -> None:
        assert not is_inactive_memory({"valid_until": FUTURE.isoformat()}, as_of=ANCHOR)

    def test_absent_is_active_open_interval(self) -> None:
        assert not is_inactive_memory({"name": "x"}, as_of=ANCHOR)

    def test_malformed_is_active_fail_open(self) -> None:
        assert not is_inactive_memory({"valid_until": "nope"}, as_of=ANCHOR)

    def test_valid_from_alone_does_not_gate(self) -> None:
        # Slice 1 keys ONLY on valid_until; a past valid_from is not inactive.
        assert not is_inactive_memory({"valid_from": PAST.isoformat()}, as_of=ANCHOR)

    def test_superseded_still_inactive_regardless_of_dates(self) -> None:
        # The #191 disjuncts are unchanged and independent of #308.
        assert is_inactive_memory(
            {"superseded_by": "winner", "valid_until": FUTURE.isoformat()},
            as_of=ANCHOR,
        )
        assert is_inactive_memory(
            {"deprecated": True, "valid_until": FUTURE.isoformat()}, as_of=ANCHOR
        )

    def test_as_of_rewind(self) -> None:
        # A claim expired relative to today but active as-of a past date.
        meta = {"valid_until": "2026-03-01"}
        assert is_inactive_memory(meta, as_of=date(2026, 6, 1))  # after -> inactive
        assert not is_inactive_memory(meta, as_of=date(2026, 2, 1))  # before -> active


# ---------------------------------------------------------------------------
# AutoMemoryFile.is_inactive (dataclass path) — the C3 compile predicate
# ---------------------------------------------------------------------------


def _am(**kw: object) -> AutoMemoryFile:
    return AutoMemoryFile(
        path=Path("raw/auto-memory/_unscoped/project_x.md"),
        origin_scope="_unscoped",
        memory_type="project",
        name="x",
        **kw,
    )


class TestIsInactiveDataclass:
    def test_past_valid_until_is_inactive(self) -> None:
        assert _am(valid_until=PAST.isoformat()).is_inactive(as_of=ANCHOR)

    def test_future_valid_until_is_active(self) -> None:
        assert not _am(valid_until=FUTURE.isoformat()).is_inactive(as_of=ANCHOR)

    def test_absent_is_active(self) -> None:
        assert not _am().is_inactive(as_of=ANCHOR)

    def test_malformed_is_active_fail_open(self) -> None:
        assert not _am(valid_until="garbage").is_inactive(as_of=ANCHOR)

    def test_superseded_marker_still_wins(self) -> None:
        assert _am(superseded_by="winner", valid_until=FUTURE.isoformat()).is_inactive(
            as_of=ANCHOR
        )


# ---------------------------------------------------------------------------
# Lockstep parity — dict and dataclass predicates must agree
# ---------------------------------------------------------------------------


class TestLockstepParity:
    @pytest.mark.parametrize(
        "raw",
        [
            # str forms
            PAST.isoformat(),
            FUTURE.isoformat(),
            ANCHOR.isoformat(),
            "",
            "garbage",
            "2026-13-40",
            # date object (YAML parses a bare YYYY-MM-DD into one)
            PAST,
            FUTURE,
            # datetime with a time component (YAML `2026-06-30 12:00:00`) —
            # str(raw) is NOT fromisoformat-parseable, so a naive store would
            # fail-open on the dataclass path while the dict path .date()s it.
            datetime(2020, 6, 30, 12, 0, 0),
            datetime(2999, 6, 30, 12, 0, 0),
            # int (YAML `20260630`) — str(raw) parses to a bogus date on the
            # dataclass path while the dict path returns None (active).
            20200630,
            20990630,
        ],
    )
    def test_dict_and_dataclass_agree(self, raw: object) -> None:
        # Build meta the way production does, and derive the dataclass field
        # via the SAME validity_bound_str used at construction — this is the
        # real invariant: is_inactive_memory(meta) == the dataclass verdict for
        # a member constructed from that meta.
        meta = {"valid_until": raw} if raw != "" else {}
        dict_verdict = is_inactive_memory(meta, as_of=ANCHOR)
        stored = validity_bound_str(meta, "valid_until")
        am_verdict = _am(valid_until=stored).is_inactive(as_of=ANCHOR)
        assert dict_verdict == am_verdict

    def test_datetime_agreement_is_not_accidental(self) -> None:
        # Regression pin for the specific divergence Quine flagged: an expired
        # datetime must be inactive on BOTH paths (not fail-open on one).
        meta = {"valid_until": datetime(2020, 6, 30, 12, 0, 0)}
        assert is_inactive_memory(meta, as_of=ANCHOR) is True
        stored = validity_bound_str(meta, "valid_until")
        assert _am(valid_until=stored).is_inactive(as_of=ANCHOR) is True

    def test_int_agreement_is_not_accidental(self) -> None:
        # An int valid_until is unparseable => fail-open (active) on BOTH paths.
        meta = {"valid_until": 20200630}
        assert is_inactive_memory(meta, as_of=ANCHOR) is False
        stored = validity_bound_str(meta, "valid_until")
        assert _am(valid_until=stored).is_inactive(as_of=ANCHOR) is False


# ---------------------------------------------------------------------------
# validity_bound_str — raw storage preserves parity even on bad dates
# ---------------------------------------------------------------------------


class TestValidityBoundStr:
    def test_str_from_yaml_date_reparses_equal(self) -> None:
        meta = {"valid_until": date(2026, 6, 30)}
        stored = validity_bound_str(meta, "valid_until")
        assert stored == "2026-06-30"
        # Stored string reparses to the same date the dict path sees.
        assert parse_valid_until({"valid_until": stored}) == parse_valid_until(meta)

    def test_datetime_with_time_normalized_and_reparses_equal(self) -> None:
        # The bound Quine flagged: a datetime must normalize to a bare ISO date
        # so the stored string reparses to exactly what the dict path computes.
        meta = {"valid_until": datetime(2026, 6, 30, 12, 0, 0)}
        stored = validity_bound_str(meta, "valid_until")
        assert stored == "2026-06-30"
        assert parse_valid_until({"valid_until": stored}) == parse_valid_until(meta)

    def test_int_normalizes_to_empty_fail_open(self) -> None:
        # An int is unparseable to a date => normalized to "" (fail-open),
        # matching the dict path's None.
        assert validity_bound_str({"valid_until": 20260630}, "valid_until") == ""

    def test_malformed_normalizes_to_empty_fail_open(self) -> None:
        # A malformed string normalizes to "" — the stored bound reparses to
        # None, exactly as the dict path parses the raw "not-a-date" to None.
        meta = {"valid_until": "not-a-date"}
        assert validity_bound_str(meta, "valid_until") == ""
        assert parse_valid_until({"valid_until": ""}) == parse_valid_until(meta)

    def test_absent_is_empty(self) -> None:
        assert validity_bound_str({}, "valid_until") == ""
        assert validity_bound_str(None, "valid_from") == ""


# ---------------------------------------------------------------------------
# Round-trip + real compile-filter path via discover_auto_memory_files
# ---------------------------------------------------------------------------


@pytest.fixture
def temporal_root(tmp_path: Path) -> Path:
    """A knowledge root with three auto-memory members:

    - ``valid`` — future ``valid_until`` (active),
    - ``expired`` — past ``valid_until`` (inactive by default),
    - ``open`` — no validity bounds (active, backward-compat).
    """
    knowledge_root = tmp_path / "knowledge"
    auto = knowledge_root / "raw" / "auto-memory"
    scope = auto / "_unscoped"
    scope.mkdir(parents=True)
    (knowledge_root / "athenaeum.yaml").write_text(
        "recall:\n  extra_intake_roots:\n    - raw/auto-memory\n",
        encoding="utf-8",
    )

    (scope / "project_valid_target.md").write_text(
        "---\nname: valid-target\ntype: project\n"
        "valid_from: 2026-01-01\nvalid_until: 2999-12-31\n---\nStill valid.\n",
        encoding="utf-8",
    )
    (scope / "project_expired_target.md").write_text(
        "---\nname: expired-target\ntype: project\n"
        "valid_from: 2020-01-01\nvalid_until: 2020-06-30\n---\nExpired.\n",
        encoding="utf-8",
    )
    (scope / "project_open_target.md").write_text(
        "---\nname: open-target\ntype: project\n---\nNo bounds.\n",
        encoding="utf-8",
    )
    return knowledge_root


class TestDiscoverRoundTripAndFilter:
    def test_fields_round_trip_into_dataclass(self, temporal_root: Path) -> None:
        from athenaeum.librarian import discover_auto_memory_files

        files = discover_auto_memory_files(temporal_root)
        by_name = {f.path.name: f for f in files}
        valid = by_name["project_valid_target.md"]
        assert valid.valid_from == "2026-01-01"
        assert valid.valid_until == "2999-12-31"
        expired = by_name["project_expired_target.md"]
        assert expired.valid_until == "2020-06-30"
        open_page = by_name["project_open_target.md"]
        assert open_page.valid_from == ""
        assert open_page.valid_until == ""

    def test_discovery_leaves_bytes_unchanged(self, temporal_root: Path) -> None:
        # Tier0/C1 discovery is read-only: the raw member is byte-for-byte
        # preserved on disk (the round-trip contract).
        from athenaeum.librarian import discover_auto_memory_files

        member = (
            temporal_root
            / "raw"
            / "auto-memory"
            / "_unscoped"
            / "project_valid_target.md"
        )
        before = member.read_bytes()
        discover_auto_memory_files(temporal_root)
        assert member.read_bytes() == before

    def test_compile_filter_excludes_expired(self, temporal_root: Path) -> None:
        # Drive the REAL compile filter: the same list comprehension C3 uses
        # in merge.py (``[am for am in files if not am.is_inactive()]``).
        from athenaeum.librarian import discover_auto_memory_files

        files = discover_auto_memory_files(temporal_root)
        active = [am for am in files if not am.is_inactive(as_of=ANCHOR)]
        active_names = {am.name for am in active}
        assert "expired-target" not in active_names
        assert "valid-target" in active_names
        assert "open-target" in active_names


class TestRealRecallPath:
    """Drive the actual recall index (FTS5), not a mock, to prove an
    expired-``valid_until`` page is excluded from recall while a still-valid
    one is returned. The index-build gate (``search.py`` L286) calls
    ``is_inactive_memory`` with the default today, so past bounds are dropped.
    """

    @pytest.fixture
    def recall_wiki(self, tmp_path: Path) -> Path:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        # The shared term "deployment" lives in the DESCRIPTION (an indexed
        # FTS5 field; the body is not indexed) so one query matches all three.
        (wiki / "valid-deploy.md").write_text(
            "---\nname: Valid Deploy Policy\ntype: reference\n"
            "description: deployment target policy\n"
            "valid_until: 2999-12-31\n---\n\nBody.\n",
            encoding="utf-8",
        )
        (wiki / "expired-deploy.md").write_text(
            "---\nname: Expired Deploy Policy\ntype: reference\n"
            "description: deployment target policy\n"
            "valid_until: 2020-06-30\n---\n\nBody.\n",
            encoding="utf-8",
        )
        (wiki / "open-deploy.md").write_text(
            "---\nname: Open Deploy Policy\ntype: reference\n"
            "description: deployment target policy\n---\n\nBody.\n",
            encoding="utf-8",
        )
        return wiki

    def test_expired_excluded_from_recall(
        self, recall_wiki: Path, tmp_path: Path
    ) -> None:
        cache = tmp_path / "cache"
        backend = FTS5Backend()
        # Only the two non-expired pages should be indexed.
        count = backend.build_index(recall_wiki, cache)
        assert count == 2

        results = backend.query("deployment", cache, n=10, wiki_root=recall_wiki)
        filenames = {r[0] for r in results}
        assert "expired-deploy.md" not in filenames
        assert "valid-deploy.md" in filenames
        assert "open-deploy.md" in filenames


# ---------------------------------------------------------------------------
# Slice 3 — the lower bound (valid_from) stays UNGATED (regression pin for #324)
# ---------------------------------------------------------------------------


class TestLowerBoundUngated:
    """``valid_from`` must NOT gate the active predicate at any ``as_of``.

    Gating it would hide a future-dated claim and break #324's disjoint-validity
    detector short-circuit, which relies on a member whose window starts after
    today staying active. See ``is_inactive_memory``'s docstring / §8.3.
    """

    def test_future_valid_from_still_active_dict(self) -> None:
        assert not is_inactive_memory({"valid_from": FUTURE.isoformat()}, as_of=ANCHOR)

    def test_future_valid_from_still_active_dataclass(self) -> None:
        assert not _am(valid_from=FUTURE.isoformat()).is_inactive(as_of=ANCHOR)

    def test_valid_from_never_flips_the_verdict(self) -> None:
        # Only the upper bound (here: open => active) decides; adding any
        # valid_from must not change the verdict at any as_of.
        for as_of in (PAST, ANCHOR, FUTURE):
            assert (
                is_inactive_memory({"valid_from": FUTURE.isoformat()}, as_of=as_of)
                is False
            )

    def test_upper_bound_still_decides_with_a_lower_bound_present(self) -> None:
        # A closed window: verdict is driven ENTIRELY by valid_until, even when
        # as_of precedes valid_from (which, if gated, would read inactive).
        meta = {"valid_from": "2026-05-01", "valid_until": "2026-06-30"}
        # Before valid_from but before valid_until too -> active (lower ungated).
        assert not is_inactive_memory(meta, as_of=date(2026, 4, 1))
        # Inside the window -> active.
        assert not is_inactive_memory(meta, as_of=date(2026, 6, 1))
        # After valid_until -> inactive (upper bound expired).
        assert is_inactive_memory(meta, as_of=date(2026, 7, 1))


# ---------------------------------------------------------------------------
# Slice 3 — supersession-as-interval vs the old tombstone
# ---------------------------------------------------------------------------


class TestIntervalVsTombstone:
    def test_tombstone_inactive_regardless_of_as_of(self) -> None:
        # superseded_by is a flat tombstone: inactive at EVERY as_of, even one
        # before the (future) valid_until would otherwise keep it live.
        meta = {"superseded_by": "winner", "valid_until": FUTURE.isoformat()}
        assert is_inactive_memory(meta, as_of=PAST)
        assert is_inactive_memory(meta, as_of=ANCHOR)
        assert is_inactive_memory(meta, as_of=FUTURE)

    def test_interval_close_is_time_sensitive(self) -> None:
        # A pure interval close (valid_until, no tombstone) flips with as_of:
        # live before the close date, inactive after — the whole point of #308.
        meta = {"valid_until": "2026-03-01"}
        assert not is_inactive_memory(meta, as_of=date(2026, 2, 1))  # before -> live
        assert is_inactive_memory(meta, as_of=date(2026, 4, 1))  # after -> inactive


# ---------------------------------------------------------------------------
# Slice 3 — interval overlap / adjacency (validity_windows_disjoint, #324)
# ---------------------------------------------------------------------------


class TestIntervalOverlapAdjacency:
    def test_clearly_disjoint(self) -> None:
        a = {"valid_from": "2026-01-01", "valid_until": "2026-03-31"}
        b = {"valid_from": "2026-04-01", "valid_until": "2026-06-30"}
        assert validity_windows_disjoint(a, b)
        assert validity_windows_disjoint(b, a)  # symmetric

    def test_adjacent_shared_boundary_day_is_not_disjoint(self) -> None:
        # A ends on the SAME inclusive day B begins -> they share that day ->
        # NOT disjoint (strict `<` on the inclusive valid_until).
        a = {"valid_from": "2026-01-01", "valid_until": "2026-04-01"}
        b = {"valid_from": "2026-04-01", "valid_until": "2026-06-30"}
        assert not validity_windows_disjoint(a, b)

    def test_overlapping_windows_not_disjoint(self) -> None:
        a = {"valid_from": "2026-01-01", "valid_until": "2026-05-01"}
        b = {"valid_from": "2026-04-01", "valid_until": "2026-08-01"}
        assert not validity_windows_disjoint(a, b)

    def test_open_bounds_never_disjoint(self) -> None:
        # An open upper bound on A means A never provably ends before B begins.
        a = {"valid_from": "2026-01-01"}  # open upper
        b = {"valid_from": "2027-01-01", "valid_until": "2027-06-30"}
        assert not validity_windows_disjoint(a, b)
        assert not validity_windows_disjoint({}, {})


# ---------------------------------------------------------------------------
# Slice 3 — as-of view through the real search backends
# ---------------------------------------------------------------------------


@pytest.fixture
def asof_wiki(tmp_path: Path) -> Path:
    """A wiki with three pages closed at different dates, sharing an indexed term.

    The as-of rewind operates through the UPPER bound (``valid_until``):

    - ``early`` — valid_until 2026-03-01 (live only through Feb).
    - ``mid``   — valid_until 2026-06-01 (live through May).
    - ``always``— no bounds (live at any as_of).
    """
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    (wiki / "early.md").write_text(
        "---\nname: Early Policy\ntype: reference\n"
        "description: rollout policy\nvalid_until: 2026-03-01\n---\n\nBody.\n",
        encoding="utf-8",
    )
    (wiki / "mid.md").write_text(
        "---\nname: Mid Policy\ntype: reference\n"
        "description: rollout policy\nvalid_until: 2026-06-01\n---\n\nBody.\n",
        encoding="utf-8",
    )
    (wiki / "always.md").write_text(
        "---\nname: Always Policy\ntype: reference\n"
        "description: rollout policy\n---\n\nBody.\n",
        encoding="utf-8",
    )
    return wiki


class TestFTS5AsOfIndex:
    def test_as_of_rewind_includes_then_live_pages(
        self, asof_wiki: Path, tmp_path: Path
    ) -> None:
        backend = FTS5Backend()
        cache = tmp_path / "asof-feb"
        # As of 2026-02-01 everything is still within its window -> all 3.
        count = backend.build_index(asof_wiki, cache, as_of=date(2026, 2, 1))
        assert count == 3
        names = {r[0] for r in backend.query("rollout", cache, n=10)}
        assert names == {"early.md", "mid.md", "always.md"}

    def test_as_of_between_closes_drops_the_earlier_page(
        self, asof_wiki: Path, tmp_path: Path
    ) -> None:
        backend = FTS5Backend()
        cache = tmp_path / "asof-apr"
        # As of 2026-04-01: ``early`` has expired, ``mid`` is still live.
        count = backend.build_index(asof_wiki, cache, as_of=date(2026, 4, 1))
        assert count == 2
        names = {r[0] for r in backend.query("rollout", cache, n=10)}
        assert names == {"mid.md", "always.md"}

    def test_as_of_after_both_closes_leaves_only_open_page(
        self, asof_wiki: Path, tmp_path: Path
    ) -> None:
        backend = FTS5Backend()
        cache = tmp_path / "asof-jul"
        # As of 2026-07-01 both dated pages are expired — only ``always``.
        count = backend.build_index(asof_wiki, cache, as_of=date(2026, 7, 1))
        assert count == 1
        names = {r[0] for r in backend.query("rollout", cache, n=10)}
        assert names == {"always.md"}


class TestKeywordAsOfQuery:
    def test_keyword_honors_as_of_at_query_time(self, asof_wiki: Path) -> None:
        # The keyword backend scans on query, so a single wiki serves every
        # as_of view with no index rebuild.
        backend = KeywordBackend()
        cache = Path("/unused")

        feb = {
            r[0]
            for r in backend.query(
                "rollout", cache, n=10, wiki_root=asof_wiki, as_of=date(2026, 2, 1)
            )
        }
        assert feb == {"early.md", "mid.md", "always.md"}

        apr = {
            r[0]
            for r in backend.query(
                "rollout", cache, n=10, wiki_root=asof_wiki, as_of=date(2026, 4, 1)
            )
        }
        assert apr == {"mid.md", "always.md"}


# ---------------------------------------------------------------------------
# Slice 3 — round-trip serialization of both bounds
# ---------------------------------------------------------------------------


class TestBoundRoundTrip:
    def test_both_bounds_round_trip_and_verdict_parity(self) -> None:
        # Bounds stored on the dataclass reparse to the SAME verdict the dict
        # path reaches for the same meta, at multiple as_of points.
        meta = {"valid_from": "2026-05-01", "valid_until": "2026-06-30"}
        am = _am(
            valid_from=validity_bound_str(meta, "valid_from"),
            valid_until=validity_bound_str(meta, "valid_until"),
        )
        assert am.valid_from == "2026-05-01"
        assert am.valid_until == "2026-06-30"
        for as_of in (date(2026, 4, 1), date(2026, 6, 1), date(2026, 8, 1)):
            assert am.is_inactive(as_of=as_of) == is_inactive_memory(meta, as_of=as_of)


# ---------------------------------------------------------------------------
# Slice 4 — per-claim (vs per-page) compiled validity
# ---------------------------------------------------------------------------


def _write_member(
    scope_dir: Path,
    filename: str,
    *,
    session: str,
    turn: int,
    body: str,
    valid_from: str = "",
    valid_until: str = "",
) -> Path:
    """Write a raw auto-memory member with sources[] + optional validity bounds."""
    scope_dir.mkdir(parents=True, exist_ok=True)
    path = scope_dir / filename
    lines = [
        "---",
        "name: deploy-target",
        "type: project",
        f"originSessionId: {session}",
        f"originTurn: {turn}",
    ]
    if valid_from:
        lines.append(f"valid_from: {valid_from}")
    if valid_until:
        lines.append(f"valid_until: {valid_until}")
    lines += [
        "sources:",
        f"  - session: {session}",
        f"    turn: {turn}",
        "---",
        body,
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


class TestPerClaimCompiledValiditySlice4:
    """Issue #308 slice 4 — a claim's validity window travels into the compiled entry."""

    # -- _stamp_member_validity: only-fill-never-override -------------------

    def test_stamp_fills_absent_bounds_from_member(self) -> None:
        from athenaeum.merge import _stamp_member_validity

        src: dict[str, object] = {"session": "s", "turn": 1}
        am = _am(valid_from="2026-04-01", valid_until="2026-06-30")
        _stamp_member_validity(src, am)
        assert src["valid_from"] == "2026-04-01"
        assert src["valid_until"] == "2026-06-30"

    def test_stamp_does_not_override_explicit_source_bound(self) -> None:
        from athenaeum.merge import _stamp_member_validity

        src: dict[str, object] = {"valid_from": "2025-01-01"}
        am = _am(valid_from="2026-04-01", valid_until="2026-06-30")
        _stamp_member_validity(src, am)
        # Explicit source lower bound preserved; only the absent upper is filled.
        assert src["valid_from"] == "2025-01-01"
        assert src["valid_until"] == "2026-06-30"

    def test_stamp_open_member_adds_no_key(self) -> None:
        from athenaeum.merge import _stamp_member_validity

        src: dict[str, object] = {"session": "s"}
        _stamp_member_validity(src, _am())  # no bounds
        assert "valid_from" not in src
        assert "valid_until" not in src

    # -- _validity_window_phrase -------------------------------------------

    def test_window_phrase_both_bounds(self) -> None:
        from athenaeum.merge import _validity_window_phrase

        phrase = _validity_window_phrase(
            {"valid_from": "2026-04-01", "valid_until": "2026-06-30"}
        )
        assert phrase == "2026-04-01 to 2026-06-30"

    def test_window_phrase_lower_only(self) -> None:
        from athenaeum.merge import _validity_window_phrase

        assert (
            _validity_window_phrase({"valid_from": "2026-04-01"}) == "from 2026-04-01"
        )

    def test_window_phrase_upper_only(self) -> None:
        from athenaeum.merge import _validity_window_phrase

        assert (
            _validity_window_phrase({"valid_until": "2026-06-30"}) == "until 2026-06-30"
        )

    def test_window_phrase_open_is_empty(self) -> None:
        from athenaeum.merge import _validity_window_phrase

        assert _validity_window_phrase({"session": "s"}) == ""

    # -- _parse_one_source round-trip --------------------------------------

    def test_parse_one_source_carries_bounds(self) -> None:
        from athenaeum.merge import _parse_one_source

        parsed = _parse_one_source(
            {
                "session": "abc",
                "turn": 2,
                "valid_from": "2026-04-01",
                "valid_until": "2026-06-30",
            },
            "scope",
        )
        assert parsed is not None
        assert parsed["valid_from"] == "2026-04-01"
        assert parsed["valid_until"] == "2026-06-30"

    def test_parse_one_source_malformed_bound_dropped(self) -> None:
        from athenaeum.merge import _parse_one_source

        parsed = _parse_one_source(
            {"session": "abc", "turn": 2, "valid_until": "garbage"}, "scope"
        )
        assert parsed is not None
        assert "valid_until" not in parsed  # fail-open: unparseable → no key

    def test_parse_one_source_no_bounds_omits_keys(self) -> None:
        from athenaeum.merge import _parse_one_source

        parsed = _parse_one_source({"session": "abc", "turn": 2}, "scope")
        assert parsed is not None
        assert "valid_from" not in parsed
        assert "valid_until" not in parsed

    # -- footnote rendering ------------------------------------------------

    def test_footnote_renders_validity_window(self) -> None:
        from athenaeum.merge import render_source_footnotes

        footnotes = render_source_footnotes(
            [
                {
                    "session": "abc",
                    "turn": 2,
                    "source_type": "user-stated",
                    "source_ref": "abc#turn2",
                    "valid_from": "2026-04-01",
                    "valid_until": "2026-06-30",
                }
            ]
        )
        assert "**Valid:** 2026-04-01 to 2026-06-30" in footnotes

    def test_footnote_without_window_unchanged(self) -> None:
        from athenaeum.merge import render_source_footnotes

        footnotes = render_source_footnotes(
            [{"session": "abc", "turn": 2, "source_type": "user-stated"}]
        )
        assert "**Valid:**" not in footnotes
        assert "[^src-1]:" in footnotes

    # -- full frontmatter round-trip ---------------------------------------

    def test_bounds_round_trip_through_frontmatter(self) -> None:
        from athenaeum.merge import (
            MergedWikiEntry,
            _parse_one_source,
            render_merged_entry,
        )
        from athenaeum.models import parse_frontmatter

        entry = MergedWikiEntry(
            topic_slug="t",
            cluster_id="c-1",
            cluster_centroid_score=1.0,
            contradictions_detected=False,
            sources=[
                {
                    "session": "abc",
                    "turn": 2,
                    "source_type": "user-stated",
                    "source_ref": "abc#turn2",
                    "valid_from": "2026-04-01",
                    "valid_until": "2026-06-30",
                }
            ],
            body="Body.\n",
        )
        out = render_merged_entry(entry)
        meta, _ = parse_frontmatter(out)
        reparsed = _parse_one_source(meta["sources"][0], "scope")
        assert reparsed is not None
        assert reparsed["valid_from"] == "2026-04-01"
        assert reparsed["valid_until"] == "2026-06-30"

    # -- integration: merge_cluster_row stamps the member window -----------

    def test_merge_cluster_row_stamps_member_window_onto_sources(
        self, tmp_path: Path
    ) -> None:
        from athenaeum.merge import merge_cluster_row, render_source_footnotes

        root = tmp_path / "raw" / "auto-memory"
        scope = root / "proj"
        _write_member(
            scope,
            "deploy_target.md",
            session="sess-1",
            turn=1,
            body="Deploy target is us-east-1.",
            valid_from="2026-04-01",
            valid_until="2099-12-31",  # future upper → member stays active
        )
        row = {
            "cluster_id": "c-1",
            "member_paths": ["proj/deploy_target.md"],
            "centroid_score": 1.0,
        }
        entry = merge_cluster_row(row, extra_roots=[root], am_by_path={})
        assert entry is not None
        assert len(entry.sources) == 1
        assert entry.sources[0]["valid_from"] == "2026-04-01"
        assert entry.sources[0]["valid_until"] == "2099-12-31"
        footnotes = render_source_footnotes(entry.sources)
        assert "**Valid:** 2026-04-01 to 2099-12-31" in footnotes

    def test_expired_member_still_dropped_no_regression(self, tmp_path: Path) -> None:
        """Slice-1/3 page-level filter is unchanged: an expired member is dropped whole."""
        from athenaeum.merge import merge_cluster_row

        root = tmp_path / "raw" / "auto-memory"
        scope = root / "proj"
        _write_member(
            scope,
            "old_target.md",
            session="sess-old",
            turn=1,
            body="Deploy target was eu-west-1.",
            valid_until="2020-01-01",  # past upper → inactive, excluded from compile
        )
        row = {
            "cluster_id": "c-2",
            "member_paths": ["proj/old_target.md"],
            "centroid_score": 1.0,
        }
        # Only member is inactive → whole row skipped (no live claim to compile).
        assert merge_cluster_row(row, extra_roots=[root], am_by_path={}) is None
