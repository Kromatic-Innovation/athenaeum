# SPDX-License-Identifier: Apache-2.0
"""Tests for ``athenaeum.dedupe`` — HIGH-confidence person dedupe + merge.

Covers the four cwc-script signals (apollo_id / linkedin / name_exact),
the per-claim ``field_sources`` preservation contract from #90, and
idempotency of repeated ``--apply`` runs.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from athenaeum.dedupe import (
    DuplicatePair,
    find_duplicate_persons,
    merge_duplicate_persons,
    pairs_from_yaml,
    pairs_to_yaml,
)
from athenaeum.models import parse_frontmatter


def _write_person(
    wiki_root: Path,
    *,
    uid: str,
    name: str,
    apollo_id: str = "",
    linkedin_url: str = "",
    emails: list[str] | None = None,
    source: str | None = None,
    field_sources: dict | None = None,
    extra: dict | None = None,
) -> Path:
    lines = ["---", f"uid: {uid}", "type: person", f"name: {name}"]
    if apollo_id:
        lines.append(f"apollo_id: {apollo_id}")
    if linkedin_url:
        lines.append(f"linkedin_url: {linkedin_url}")
    if emails:
        lines.append("emails:")
        for e in emails:
            lines.append(f"  - {e}")
    if source is not None:
        lines.append(f"source: {source}")
    if field_sources is not None:
        import yaml as _yaml

        lines.append("field_sources:")
        for k, v in field_sources.items():
            # render as nested map
            lines.append(f"  {k}: {_yaml.safe_dump(v).strip()}")
    if extra:
        for k, v in extra.items():
            lines.append(f"{k}: {v}")
    lines.append("---")
    lines.append("")
    lines.append(f"Body for {name}.")
    path = wiki_root / f"{uid}-{name.lower().replace(' ', '-')}.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


@pytest.fixture
def wiki_root(tmp_path: Path) -> Path:
    root = tmp_path / "wiki"
    root.mkdir()
    return root


class TestFindDuplicatePersons:
    def test_apollo_id_match(self, wiki_root: Path) -> None:
        _write_person(wiki_root, uid="aaaa1111", name="Alice Smith", apollo_id="apo-1")
        _write_person(wiki_root, uid="aaaa2222", name="Alice S.", apollo_id="apo-1")
        pairs = find_duplicate_persons(wiki_root)
        assert len(pairs) == 1
        assert pairs[0].match_signal == "apollo_id"
        assert pairs[0].confidence == "HIGH"
        assert {pairs[0].canonical_uid, pairs[0].absorbed_uid} == {
            "aaaa1111",
            "aaaa2222",
        }

    def test_linkedin_match(self, wiki_root: Path) -> None:
        _write_person(
            wiki_root,
            uid="bbbb1111",
            name="Bob Jones",
            linkedin_url="https://www.linkedin.com/in/bobjones/",
        )
        _write_person(
            wiki_root,
            uid="bbbb2222",
            name="Robert Jones",
            linkedin_url="https://linkedin.com/in/bobjones",
        )
        pairs = find_duplicate_persons(wiki_root)
        assert len(pairs) == 1
        assert pairs[0].match_signal == "linkedin_url"

    def test_name_exact_match(self, wiki_root: Path) -> None:
        _write_person(wiki_root, uid="cccc1111", name="Carol Diaz")
        _write_person(wiki_root, uid="cccc2222", name="Dr. Carol Diaz")
        pairs = find_duplicate_persons(wiki_root)
        assert len(pairs) == 1
        assert pairs[0].match_signal == "name_exact"

    def test_no_duplicates(self, wiki_root: Path) -> None:
        _write_person(wiki_root, uid="dddd1111", name="Eve Adams")
        _write_person(wiki_root, uid="dddd2222", name="Frank Brown")
        assert find_duplicate_persons(wiki_root) == []

    def test_common_name_4plus_dropped(self, wiki_root: Path) -> None:
        # 4 wikis with the same normalized name → ambiguous, dropped from
        # the HIGH-only public API.
        for i in range(4):
            _write_person(wiki_root, uid=f"eeee000{i}", name="John Smith")
        assert find_duplicate_persons(wiki_root) == []


class TestMergePreservesFieldSources:
    def test_list_union_preserves_per_value_attribution(self, wiki_root: Path) -> None:
        cpath = _write_person(
            wiki_root,
            uid="11111111",
            name="Alice Canon",
            apollo_id="apo-x",
            emails=["alice@canonical.com"],
            field_sources={"emails": "google:contact-1"},
        )
        apath = _write_person(
            wiki_root,
            uid="22222222",
            name="Alice Absorb",
            apollo_id="apo-x",
            emails=["alice@absorbed.com"],
            field_sources={"emails": "linkedin:profile-2"},
        )
        # Force canonical = 11111111 by giving it more emails / apollo
        pair = DuplicatePair(
            canonical_uid="11111111",
            absorbed_uid="22222222",
            match_signal="apollo_id",
            canonical_path=str(cpath),
            absorbed_path=str(apath),
        )
        report = merge_duplicate_persons([pair], apply=True)
        assert report.merged == 1
        meta, _body = parse_frontmatter(cpath.read_text(encoding="utf-8"))
        # Union of emails preserved
        assert "alice@canonical.com" in meta["emails"]
        assert "alice@absorbed.com" in meta["emails"]
        # Canonical's field_sources entry wins on the key (canonical-first
        # ordering matches scalar-source semantics; per-value attribution
        # is documented as the canonical-first list rule).
        assert meta["field_sources"]["emails"] == "google:contact-1"

    def test_absorbed_only_field_sources_carries_forward(self, wiki_root: Path) -> None:
        cpath = _write_person(
            wiki_root,
            uid="33333333",
            name="Bob Canon",
            apollo_id="apo-y",
            emails=["bob@canonical.com"],
            source="claude:session-canon",
        )
        apath = _write_person(
            wiki_root,
            uid="44444444",
            name="Bob Absorb",
            apollo_id="apo-y",
            emails=["bob@absorbed.com"],
            source="apollo:export-2026",
            field_sources={"emails": "apollo:export-2026"},
        )
        pair = DuplicatePair(
            canonical_uid="33333333",
            absorbed_uid="44444444",
            match_signal="apollo_id",
            canonical_path=str(cpath),
            absorbed_path=str(apath),
        )
        merge_duplicate_persons([pair], apply=True)
        meta, _ = parse_frontmatter(cpath.read_text(encoding="utf-8"))
        # Absorbed-only emails attribution carried forward
        assert meta["field_sources"]["emails"] == "apollo:export-2026"
        # Wiki-level source: canonical wins
        assert meta["source"] == "claude:session-canon"
        # Absorbed source archived in audit trail
        assert meta["merged_from_sources"]["44444444"] == "apollo:export-2026"
        # Audit trail uid recorded
        assert "44444444" in meta["merged_from"]


class TestIdempotency:
    def test_apply_twice_is_noop(self, wiki_root: Path) -> None:
        cpath = _write_person(
            wiki_root,
            uid="55555555",
            name="Carol Canon",
            apollo_id="apo-z",
        )
        apath = _write_person(
            wiki_root,
            uid="66666666",
            name="Carol Absorb",
            apollo_id="apo-z",
        )
        pair = DuplicatePair(
            canonical_uid="55555555",
            absorbed_uid="66666666",
            match_signal="apollo_id",
            canonical_path=str(cpath),
            absorbed_path=str(apath),
        )
        r1 = merge_duplicate_persons([pair], apply=True)
        assert r1.merged == 1
        assert not apath.exists()
        # Second run: absorbed is gone → already_merged path.
        r2 = merge_duplicate_persons([pair], apply=True)
        assert r2.merged == 0
        assert r2.already_merged == 1


class TestReportRoundtrip:
    def test_yaml_roundtrip(self, wiki_root: Path) -> None:
        _write_person(wiki_root, uid="77777777", name="Dee", apollo_id="apo-q")
        _write_person(wiki_root, uid="88888888", name="Dee", apollo_id="apo-q")
        pairs = find_duplicate_persons(wiki_root)
        text = pairs_to_yaml(pairs)
        roundtripped = pairs_from_yaml(text)
        assert len(roundtripped) == 1
        assert roundtripped[0].canonical_uid == pairs[0].canonical_uid
        assert roundtripped[0].absorbed_uid == pairs[0].absorbed_uid
        assert roundtripped[0].match_signal == pairs[0].match_signal


class TestDryRun:
    def test_dry_run_does_not_modify(self, wiki_root: Path) -> None:
        cpath = _write_person(
            wiki_root,
            uid="99999999",
            name="Ed",
            apollo_id="apo-r",
        )
        apath = _write_person(
            wiki_root,
            uid="aaaaaaaa",
            name="Ed",
            apollo_id="apo-r",
        )
        before_c = cpath.read_text()
        before_a = apath.read_text()
        pair = DuplicatePair(
            canonical_uid="99999999",
            absorbed_uid="aaaaaaaa",
            match_signal="apollo_id",
            canonical_path=str(cpath),
            absorbed_path=str(apath),
        )
        report = merge_duplicate_persons([pair], apply=False)
        assert report.dry_run is True
        assert report.merged == 1  # counted as would-merge
        assert cpath.read_text() == before_c
        assert apath.read_text() == before_a
