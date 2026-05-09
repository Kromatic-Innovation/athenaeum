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
    _union_list,
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


class TestSocialUrlCoalesce:
    """Regression for #106 — twitter_url / github_url were silently dropped."""

    def test_absorbed_only_social_urls_carry_forward(self, wiki_root: Path) -> None:
        cpath = _write_person(
            wiki_root,
            uid="s1111111",
            name="Social Canon",
            apollo_id="apo-soc",
        )
        apath = _write_person(
            wiki_root,
            uid="s2222222",
            name="Social Absorb",
            apollo_id="apo-soc",
            extra={
                "twitter_url": "https://twitter.com/socialabsorb",
                "github_url": "https://github.com/socialabsorb",
            },
        )
        pair = DuplicatePair(
            canonical_uid="s1111111",
            absorbed_uid="s2222222",
            match_signal="apollo_id",
            canonical_path=str(cpath),
            absorbed_path=str(apath),
        )
        merge_duplicate_persons([pair], apply=True)
        meta, _ = parse_frontmatter(cpath.read_text(encoding="utf-8"))
        assert meta["twitter_url"] == "https://twitter.com/socialabsorb"
        assert meta["github_url"] == "https://github.com/socialabsorb"

    def test_canonical_social_urls_win(self, wiki_root: Path) -> None:
        cpath = _write_person(
            wiki_root,
            uid="s3333333",
            name="Social Canon2",
            apollo_id="apo-soc2",
            extra={
                "twitter_url": "https://twitter.com/canonical",
                "github_url": "https://github.com/canonical",
            },
        )
        apath = _write_person(
            wiki_root,
            uid="s4444444",
            name="Social Absorb2",
            apollo_id="apo-soc2",
            extra={
                "twitter_url": "https://twitter.com/absorbed",
                "github_url": "https://github.com/absorbed",
            },
        )
        pair = DuplicatePair(
            canonical_uid="s3333333",
            absorbed_uid="s4444444",
            match_signal="apollo_id",
            canonical_path=str(cpath),
            absorbed_path=str(apath),
        )
        merge_duplicate_persons([pair], apply=True)
        meta, _ = parse_frontmatter(cpath.read_text(encoding="utf-8"))
        assert meta["twitter_url"] == "https://twitter.com/canonical"
        assert meta["github_url"] == "https://github.com/canonical"


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


class TestMaxMerge:
    def test_warm_score_takes_max(self, wiki_root: Path) -> None:
        cpath = _write_person(
            wiki_root,
            uid="m1111111",
            name="Max Canon",
            apollo_id="apo-m",
            extra={
                "warm_score": 0.4,
                "last_touch": "2026-01-01",
                "updated": "2026-01-01",
            },
        )
        apath = _write_person(
            wiki_root,
            uid="m2222222",
            name="Max Absorb",
            apollo_id="apo-m",
            extra={
                "warm_score": 0.9,
                "last_touch": "2026-04-15",
                "updated": "2026-04-15",
            },
        )
        pair = DuplicatePair(
            canonical_uid="m1111111",
            absorbed_uid="m2222222",
            match_signal="apollo_id",
            canonical_path=str(cpath),
            absorbed_path=str(apath),
        )
        merge_duplicate_persons([pair], apply=True)
        meta, _ = parse_frontmatter(cpath.read_text(encoding="utf-8"))
        assert float(meta["warm_score"]) == 0.9
        assert str(meta["last_touch"]) == "2026-04-15"
        # `updated` is stamped to today on merge — must be >= absorbed's
        assert str(meta["updated"]) >= "2026-04-15"

    def test_max_keeps_canonical_when_larger(self, wiki_root: Path) -> None:
        cpath = _write_person(
            wiki_root,
            uid="m3333333",
            name="Max2 Canon",
            apollo_id="apo-m2",
            extra={"warm_score": 0.95, "last_touch": "2026-05-01"},
        )
        apath = _write_person(
            wiki_root,
            uid="m4444444",
            name="Max2 Absorb",
            apollo_id="apo-m2",
            extra={"warm_score": 0.1, "last_touch": "2025-01-01"},
        )
        pair = DuplicatePair(
            canonical_uid="m3333333",
            absorbed_uid="m4444444",
            match_signal="apollo_id",
            canonical_path=str(cpath),
            absorbed_path=str(apath),
        )
        merge_duplicate_persons([pair], apply=True)
        meta, _ = parse_frontmatter(cpath.read_text(encoding="utf-8"))
        assert float(meta["warm_score"]) == 0.95
        assert str(meta["last_touch"]) == "2026-05-01"


class TestCoalesceOnGap:
    def test_apollo_id_filled_from_absorbed(self, wiki_root: Path) -> None:
        cpath = _write_person(
            wiki_root,
            uid="g1111111",
            name="Gap Canon",
            linkedin_url="https://linkedin.com/in/gap",
        )
        apath = _write_person(
            wiki_root,
            uid="g2222222",
            name="Gap Absorb",
            apollo_id="apo-gap",
            linkedin_url="https://linkedin.com/in/gap",
        )
        pair = DuplicatePair(
            canonical_uid="g1111111",
            absorbed_uid="g2222222",
            match_signal="linkedin_url",
            canonical_path=str(cpath),
            absorbed_path=str(apath),
        )
        merge_duplicate_persons([pair], apply=True)
        meta, _ = parse_frontmatter(cpath.read_text(encoding="utf-8"))
        assert meta["apollo_id"] == "apo-gap"

    def test_linkedin_url_filled_from_absorbed(self, wiki_root: Path) -> None:
        cpath = _write_person(
            wiki_root,
            uid="g3333333",
            name="Gap2 Canon",
            apollo_id="apo-shared",
        )
        apath = _write_person(
            wiki_root,
            uid="g4444444",
            name="Gap2 Absorb",
            apollo_id="apo-shared",
            linkedin_url="https://linkedin.com/in/gap2",
        )
        pair = DuplicatePair(
            canonical_uid="g3333333",
            absorbed_uid="g4444444",
            match_signal="apollo_id",
            canonical_path=str(cpath),
            absorbed_path=str(apath),
        )
        merge_duplicate_persons([pair], apply=True)
        meta, _ = parse_frontmatter(cpath.read_text(encoding="utf-8"))
        assert meta["linkedin_url"] == "https://linkedin.com/in/gap2"


class TestBodyMerge:
    def test_body_concatenated_under_merged_from_heading(self, wiki_root: Path) -> None:
        cpath = _write_person(
            wiki_root,
            uid="b1111111",
            name="Body Canon",
            apollo_id="apo-b",
        )
        apath = _write_person(
            wiki_root,
            uid="b2222222",
            name="Body Absorb",
            apollo_id="apo-b",
        )
        pair = DuplicatePair(
            canonical_uid="b1111111",
            absorbed_uid="b2222222",
            match_signal="apollo_id",
            canonical_path=str(cpath),
            absorbed_path=str(apath),
        )
        merge_duplicate_persons([pair], apply=True)
        _meta, body = parse_frontmatter(cpath.read_text(encoding="utf-8"))
        # Canonical body preserved
        assert "Body for Body Canon." in body
        # Absorbed body appended under heading with absorbed uid
        assert "## Merged from b2222222" in body
        assert "Body for Body Absorb." in body
        # Heading appears AFTER canonical body
        assert body.index("Body for Body Canon.") < body.index(
            "## Merged from b2222222"
        )


class TestUnionList:
    def test_dedup_strings(self) -> None:
        assert _union_list(["a", "b"], ["b", "c"]) == ["a", "b", "c"]

    def test_canonical_first_order(self) -> None:
        assert _union_list(["x", "y"], ["a", "y"]) == ["x", "y", "a"]

    def test_dedup_dicts_by_repr(self) -> None:
        d1 = {"k": 1}
        d2 = {"k": 2}
        d1_dup = {"k": 1}
        out = _union_list([d1, d2], [d1_dup, {"k": 3}])
        assert len(out) == 3
        assert {"k": 1} in out
        assert {"k": 2} in out
        assert {"k": 3} in out

    def test_handles_none(self) -> None:
        assert _union_list(None, ["a"]) == ["a"]
        assert _union_list(["a"], None) == ["a"]
        assert _union_list(None, None) == []


class TestPairsFromYamlMalformed:
    def test_missing_canonical_uid_raises(self) -> None:
        bad = "- absorbed_uid: x\n  match_signal: apollo_id\n"
        with pytest.raises((ValueError, KeyError)):
            pairs_from_yaml(bad)

    def test_wrong_top_level_type_raises(self) -> None:
        with pytest.raises(ValueError, match="YAML list"):
            pairs_from_yaml("canonical_uid: foo\nabsorbed_uid: bar\n")

    def test_entry_not_dict_raises(self) -> None:
        with pytest.raises(ValueError, match="must be a dict"):
            pairs_from_yaml("- just_a_string\n- another\n")

    def test_malformed_yaml_raises(self) -> None:
        import yaml as _yaml

        with pytest.raises(_yaml.YAMLError):
            pairs_from_yaml("- canonical_uid: [unclosed\n")


class TestNonPersonFilter:
    def test_only_person_pairs_surface(self, wiki_root: Path) -> None:
        # Two real person duplicates
        _write_person(wiki_root, uid="p1111111", name="Person Dup", apollo_id="apo-p")
        _write_person(wiki_root, uid="p2222222", name="Person Dup", apollo_id="apo-p")
        # Company wikis sharing a "name" — must NOT be returned
        (wiki_root / "co1.md").write_text(
            "---\nuid: co111111\ntype: company\nname: Acme Corp\n---\n\nbody\n",
            encoding="utf-8",
        )
        (wiki_root / "co2.md").write_text(
            "---\nuid: co222222\ntype: company\nname: Acme Corp\n---\n\nbody\n",
            encoding="utf-8",
        )
        # Concept wikis sharing a name — must NOT be returned
        (wiki_root / "concept1.md").write_text(
            "---\nuid: cn111111\ntype: concept\nname: Lean Startup\n---\n\nbody\n",
            encoding="utf-8",
        )
        (wiki_root / "concept2.md").write_text(
            "---\nuid: cn222222\ntype: concept\nname: Lean Startup\n---\n\nbody\n",
            encoding="utf-8",
        )
        # Person row with empty name — must be skipped
        (wiki_root / "blank-name.md").write_text(
            '---\nuid: pblnk111\ntype: person\nname: ""\n---\n\nbody\n',
            encoding="utf-8",
        )
        # Person row with empty uid — must be skipped
        (wiki_root / "blank-uid.md").write_text(
            '---\nuid: ""\ntype: person\nname: Ghost Person\n---\n\nbody\n',
            encoding="utf-8",
        )

        pairs = find_duplicate_persons(wiki_root)
        assert len(pairs) == 1
        assert {pairs[0].canonical_uid, pairs[0].absorbed_uid} == {
            "p1111111",
            "p2222222",
        }


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
