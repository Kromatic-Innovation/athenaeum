# SPDX-License-Identifier: Apache-2.0
"""Conflict-resolution lock suite (issue #91).

Pins the CURRENT behavior of every conflict-resolving code path under
``src/athenaeum/`` as documented in ``docs/conflict-resolution.md``. These
tests are intentionally behavioral — when an audit found surprising or
buggy behavior, the test still asserts what the code DOES today, and the
surprise is filed as a separate issue (linked in the PR body).

Coverage targets the seven in-tree resolvers listed in #91:

1. ``librarian.tier0_passthrough`` — skip-on-conflict eligibility gate.
2. ``tiers.tier3_create`` — no-conflict-by-construction.
3. ``tiers.tier3_merge`` — LLM-mediated three-class taxonomy + ESCALATE.
4. ``tiers.tier3_write`` — atomic per-file apply, last-write-wins on disk.
5. ``merge.merge_cluster_row`` — sources `(session,turn)` dedupe + body
   paragraph dedupe + origin_scopes union.
6. ``contradictions.detect_contradictions`` — DETECT-ONLY, never resolves.
7. ``dedupe._perform_merge`` (+ ``_merge_meta``, ``_merge_field_sources``) —
   canonical-wins / max / union per field class with provenance carry.

Naming convention: ``test_<resolver>_<scenario>_<expected_winner>``.
"""

from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from athenaeum.contradictions import (
    ContradictionResult,
    _build_user_message,
    _member_scope_header,
    detect_contradictions,
)
from athenaeum.dedupe import (
    DuplicatePair,
    _coalesce,
    _max_date,
    _max_numeric,
    _merge_field_sources,
    _perform_merge,
    _union_list,
    merge_duplicate_persons,
)
from athenaeum.librarian import tier0_passthrough
from athenaeum.merge import (
    AUTO_WIKI_PREFIX,
    dedupe_sources,
    merge_cluster_row,
    merge_clusters_to_wiki,
    synthesize_body,
)
from athenaeum.models import (
    AutoMemoryFile,
    EntityAction,
    EntityIndex,
    RawFile,
    parse_frontmatter,
    parse_valid_until,
    render_frontmatter,
    validity_windows_disjoint,
)
from athenaeum.resolutions import (
    ResolutionProposal,
    enact_resolution,
    propose_resolution,
)
from athenaeum.tiers import tier3_create, tier3_merge, tier3_write

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_client(response_text: str) -> MagicMock:
    client = MagicMock()
    mock_response = MagicMock()
    mock_response.content = [MagicMock(text=response_text)]
    client.messages.create.return_value = mock_response
    return client


def _make_raw(content: str) -> RawFile:
    return RawFile(
        path=Path("/tmp/fake/sessions/20240407T120000Z-aabb0011.md"),
        source="sessions",
        timestamp="20240407T120000Z",
        uuid8="aabb0011",
        _content=content,
    )


def _wiki_root(tmp_path: Path) -> Path:
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    return wiki


def _person_wiki(
    wiki: Path,
    *,
    uid: str,
    name: str,
    extra: dict | None = None,
    body: str = "",
) -> Path:
    meta: dict = {"uid": uid, "type": "person", "name": name}
    if extra:
        meta.update(extra)
    path = wiki / f"{uid}-{name.lower().replace(' ', '-')}.md"
    path.write_text(render_frontmatter(meta) + body, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# 1. tier0_passthrough — skip-on-conflict eligibility gate
# ---------------------------------------------------------------------------


class TestTier0PassthroughSkipOnConflict:
    """tier0_passthrough never overwrites — it bails to None on every conflict."""

    def _make_raw_md(self, frontmatter: str, body: str = "Body.") -> RawFile:
        return _make_raw(f"---\n{frontmatter}---\n\n{body}\n")

    def test_tier0_passthrough_uid_already_in_index_skips(self, tmp_path: Path) -> None:
        wiki = _wiki_root(tmp_path)
        _person_wiki(wiki, uid="abc12345", name="Alice")
        index = EntityIndex(wiki)
        raw = self._make_raw_md("uid: abc12345\ntype: person\nname: Alice Different\n")
        result = tier0_passthrough(raw, index, wiki, ["person"])
        assert result is None

    def test_tier0_passthrough_filename_collision_different_uid_skips(
        self,
        tmp_path: Path,
    ) -> None:
        wiki = _wiki_root(tmp_path)
        # Pre-existing file at the target filename, but with a different uid
        # so the index lookup misses; the on-disk collision still bails.
        (wiki / "newuid01-alice.md").write_text("---\nuid: oldother\n---\n")
        index = EntityIndex(wiki)
        raw = self._make_raw_md("uid: newuid01\ntype: person\nname: Alice\n")
        result = tier0_passthrough(raw, index, wiki, ["person"])
        assert result is None

    def test_tier0_passthrough_invalid_type_skips(self, tmp_path: Path) -> None:
        wiki = _wiki_root(tmp_path)
        index = EntityIndex(wiki)
        raw = self._make_raw_md("uid: newuid01\ntype: not-a-type\nname: Alice\n")
        result = tier0_passthrough(raw, index, wiki, ["person"])
        assert result is None

    def test_tier0_passthrough_missing_required_skips(self, tmp_path: Path) -> None:
        wiki = _wiki_root(tmp_path)
        index = EntityIndex(wiki)
        raw = self._make_raw_md("type: person\nname: Alice\n")  # no uid
        assert tier0_passthrough(raw, index, wiki, ["person"]) is None

    def test_tier0_passthrough_eligible_writes_verbatim_incoming_wins(
        self,
        tmp_path: Path,
    ) -> None:
        wiki = _wiki_root(tmp_path)
        index = EntityIndex(wiki)
        raw = self._make_raw_md(
            "uid: newuid01\n"
            "type: person\n"
            "name: Alice\n"
            "linkedin_url: https://linkedin.com/in/alice\n"
            "field_sources:\n"
            "  linkedin_url: api:apollo:2025-01-01\n",
            body="# Alice\n\nNotes.",
        )
        entity = tier0_passthrough(raw, index, wiki, ["person"])
        assert entity is not None
        out = (wiki / "newuid01-alice.md").read_text(encoding="utf-8")
        meta, body = parse_frontmatter(out)
        # Custom-namespace fields preserved byte-for-byte (post-#90 contract)
        assert meta["linkedin_url"] == "https://linkedin.com/in/alice"
        assert meta["field_sources"] == {"linkedin_url": "api:apollo:2025-01-01"}
        # updated stamped to today; this is the documented one mutation
        assert meta["updated"] == date.today().isoformat()
        assert body.strip().startswith("# Alice")


# ---------------------------------------------------------------------------
# 2. tier3_create — no conflict by construction
# ---------------------------------------------------------------------------


class TestTier3CreateNoConflictByConstruction:
    def test_tier3_create_new_entity_no_conflict_path_runs(self) -> None:
        action = EntityAction(
            kind="create",
            name="Alice",
            entity_type="person",
            tags=["active"],
            access="internal",
            existing_uid=None,
            observations="Met Alice at conference.",
        )
        client = _mock_client("# Alice\n\nProduct lead.")
        entity = tier3_create(action, "sessions/raw.md", client)
        # tier3_create always succeeds with the LLM body; no conflict surface.
        assert entity.name == "Alice"
        assert entity.body == "# Alice\n\nProduct lead."
        assert entity.created == date.today().isoformat()
        assert entity.updated == date.today().isoformat()


# ---------------------------------------------------------------------------
# 3. tier3_merge — LLM-mediated three-class resolution
# ---------------------------------------------------------------------------


class TestTier3MergeLLMMediated:
    def test_tier3_merge_scalar_conflict_llm_decides_wins(self) -> None:
        """tier3_merge does NOT enforce incoming-wins or existing-wins; the LLM
        is told to pick by reliability for factual conflicts. Lock the contract:
        whatever body the LLM returns is what gets written, and no escalation
        is raised when there's no ESCALATE: marker."""
        action = EntityAction(
            kind="update",
            name="Acme",
            entity_type="company",
            tags=[],
            access="",
            existing_uid="a1b2c3d4",
            observations="HQ moved to Austin.",
        )
        # LLM picks "incoming wins" for the HQ field this time.
        client = _mock_client("# Acme\n\nHQ: Austin (updated).")
        body, esc = tier3_merge(action, "# Acme\n\nHQ: SF.", "ref", client)
        assert esc is None
        assert body == "# Acme\n\nHQ: Austin (updated)."

    def test_tier3_merge_principled_with_separator_returns_body_and_escalation(
        self,
    ) -> None:
        action = EntityAction(
            kind="update",
            name="X",
            entity_type="reference",
            tags=[],
            access="",
            existing_uid="uid12345",
            observations="obs",
        )
        client = _mock_client(
            "ESCALATE: Values conflict on commit policy.\n---\n# X\n\nMerged."
        )
        body, esc = tier3_merge(action, "Existing.", "ref", client)
        assert esc is not None
        assert esc.conflict_type == "principled"
        assert "commit" in esc.description.lower()
        assert body == "# X\n\nMerged."

    def test_tier3_merge_principled_without_separator_returns_none_body(
        self,
    ) -> None:
        action = EntityAction(
            kind="update",
            name="X",
            entity_type="reference",
            tags=[],
            access="",
            existing_uid="uid12345",
            observations="obs",
        )
        client = _mock_client("ESCALATE: Irreconcilable.")
        body, esc = tier3_merge(action, "Existing.", "ref", client)
        assert esc is not None
        assert body is None  # caller MUST NOT write the page


# ---------------------------------------------------------------------------
# 4. tier3_write — atomic per-file, last-write-wins across files
# ---------------------------------------------------------------------------


class TestTier3WriteAtomicity:
    def test_tier3_write_all_or_nothing_per_raw_file_atomic(
        self,
        tmp_path: Path,
    ) -> None:
        """All LLM calls run before any disk write. If we collect 2 update
        actions for 2 different existing pages, both writes happen after the
        last successful LLM call — never mid-loop."""
        wiki = _wiki_root(tmp_path)
        _person_wiki(wiki, uid="uid1aaaa", name="Alice", body="# Alice\n\nOriginal A.")
        _person_wiki(wiki, uid="uid2bbbb", name="Bob", body="# Bob\n\nOriginal B.")
        index = EntityIndex(wiki)
        raw = _make_raw("source content")

        actions = [
            EntityAction(
                kind="update",
                name="Alice",
                entity_type="person",
                tags=[],
                access="",
                existing_uid="uid1aaaa",
                observations="new info A",
            ),
            EntityAction(
                kind="update",
                name="Bob",
                entity_type="person",
                tags=[],
                access="",
                existing_uid="uid2bbbb",
                observations="new info B",
            ),
        ]
        client = MagicMock()
        client.messages.create.side_effect = [
            MagicMock(content=[MagicMock(text="# Alice\n\nUpdated A.")]),
            MagicMock(content=[MagicMock(text="# Bob\n\nUpdated B.")]),
        ]
        new_entities, updated_uids, escalations = tier3_write(
            raw,
            actions,
            index,
            wiki,
            client,
        )
        assert new_entities == []
        assert sorted(updated_uids) == ["uid1aaaa", "uid2bbbb"]
        assert escalations == []
        # Both writes landed.
        assert "Updated A" in (wiki / "uid1aaaa-alice.md").read_text()
        assert "Updated B" in (wiki / "uid2bbbb-bob.md").read_text()


# ---------------------------------------------------------------------------
# 5. merge.py — auto-memory cluster merge rules
# ---------------------------------------------------------------------------


class TestMergeClusterSourcesUnion:
    def test_dedupe_sources_session_turn_first_occurrence_wins(self) -> None:
        entries = [
            {"session": "s1", "turn": 1, "origin_scope": "a"},
            {"session": "s1", "turn": 1, "origin_scope": "b"},  # dup
            {"session": "s1", "turn": 2, "origin_scope": "a"},  # different turn
            {"session": "s2", "turn": 1, "origin_scope": "a"},  # different session
        ]
        out = dedupe_sources(entries)
        assert len(out) == 3
        assert out[0]["origin_scope"] == "a"  # first occurrence wins on (s1,1)

    def test_dedupe_sources_missing_turn_only_dedupes_within_none_turns(self) -> None:
        entries = [
            {"session": "s1"},
            {"session": "s1"},
            {"session": "s1", "turn": 1},
        ]
        out = dedupe_sources(entries)
        assert len(out) == 2  # one (s1, None) + one (s1, 1)

    def test_synthesize_body_paragraph_dedupe_first_wins(self) -> None:
        bodies = [
            ("scopeA", "a.md", "Para one.\n\nPara two."),
            ("scopeB", "b.md", "Para one.\n\nPara three."),
        ]
        out = synthesize_body(bodies)
        # "Para one." appears only once (first wins).
        assert out.count("Para one.") == 1
        assert "Para two." in out
        assert "Para three." in out

    def test_merge_cluster_row_origin_scopes_union_first_seen_order(
        self,
        tmp_path: Path,
    ) -> None:
        # Build two members in different scopes.
        scope_a = tmp_path / "scope_a"
        scope_b = tmp_path / "scope_b"
        scope_a.mkdir()
        scope_b.mkdir()
        (scope_a / "feedback_one.md").write_text(
            "---\nname: f1\ntype: feedback\nsources:\n  - {session: s1, turn: 1}\n---\n\nBody A.\n",
        )
        (scope_b / "feedback_two.md").write_text(
            "---\nname: f2\ntype: feedback\nsources:\n  - {session: s2, turn: 1}\n---\n\nBody B.\n",
        )
        am_a = AutoMemoryFile(
            path=scope_a / "feedback_one.md",
            origin_scope="scope_a",
            memory_type="feedback",
            name="f1",
        )
        am_b = AutoMemoryFile(
            path=scope_b / "feedback_two.md",
            origin_scope="scope_b",
            memory_type="feedback",
            name="f2",
        )
        am_by_path = {
            str(am_a.path.resolve()): am_a,
            str(am_b.path.resolve()): am_b,
        }
        row = {
            "cluster_id": "c1",
            "centroid_score": 0.9,
            "member_paths": [
                "scope_a/feedback_one.md",
                "scope_b/feedback_two.md",
            ],
        }
        entry = merge_cluster_row(
            row,
            extra_roots=[tmp_path],
            am_by_path=am_by_path,
        )
        assert entry is not None
        assert entry.origin_scopes == ["scope_a", "scope_b"]
        # Sources unioned + deduped on (session, turn).
        sessions = sorted(s["session"] for s in entry.sources)
        assert sessions == ["s1", "s2"]


# ---------------------------------------------------------------------------
# 6. contradictions.py — DETECT-ONLY
# ---------------------------------------------------------------------------


class TestContradictionsDetectOnly:
    def test_detect_contradictions_singleton_returns_not_detected_no_call(
        self,
        tmp_path: Path,
    ) -> None:
        am = AutoMemoryFile(
            path=tmp_path / "a.md",
            origin_scope="x",
            memory_type="feedback",
            name="solo",
        )
        client = MagicMock()
        result = detect_contradictions([am], client)
        assert result.detected is False
        assert result.rationale == "singleton"
        client.messages.create.assert_not_called()

    def test_detect_contradictions_no_client_returns_not_detected(
        self,
        tmp_path: Path,
    ) -> None:
        ams = [
            AutoMemoryFile(
                path=tmp_path / f"a{i}.md",
                origin_scope="x",
                memory_type="feedback",
                name=f"m{i}",
            )
            for i in range(2)
        ]
        result = detect_contradictions(ams, None)
        assert result.detected is False
        assert result.rationale == "llm-unavailable"

    def test_detect_contradictions_api_error_returns_not_detected(
        self,
        tmp_path: Path,
    ) -> None:
        for i in range(2):
            (tmp_path / f"a{i}.md").write_text(
                f"---\nname: m{i}\ntype: feedback\n---\n\nBody {i}.\n",
            )
        ams = [
            AutoMemoryFile(
                path=tmp_path / f"a{i}.md",
                origin_scope="x",
                memory_type="feedback",
                name=f"m{i}",
            )
            for i in range(2)
        ]
        client = MagicMock()
        client.messages.create.side_effect = RuntimeError("boom")
        result = detect_contradictions(ams, client)
        assert result.detected is False
        assert result.rationale == "llm-unavailable"

    def test_detect_contradictions_does_not_modify_input_members(
        self,
        tmp_path: Path,
    ) -> None:
        for i in range(2):
            (tmp_path / f"a{i}.md").write_text(
                f"---\nname: m{i}\ntype: feedback\n---\n\nBody {i}.\n",
            )
        ams = [
            AutoMemoryFile(
                path=tmp_path / f"a{i}.md",
                origin_scope="x",
                memory_type="feedback",
                name=f"m{i}",
            )
            for i in range(2)
        ]
        client = _mock_client(
            '{"detected": true, "conflict_type": "factual", '
            '"members_involved": ["x/a0.md", "x/a1.md"], '
            '"conflicting_passages": ["p0", "p1"], '
            '"rationale": "incompatible"}'
        )
        result = detect_contradictions(ams, client)
        # Lock: detector NEVER mutates member files / bodies.
        assert result.detected is True
        for i in range(2):
            assert (tmp_path / f"a{i}.md").read_text().endswith(f"Body {i}.\n")


# ---------------------------------------------------------------------------
# 7. dedupe._perform_merge — per-field rules + provenance carry
# ---------------------------------------------------------------------------


class TestDedupeMergePrimitives:
    def test_union_list_canonical_first_dedup_preserves_order(self) -> None:
        out = _union_list(["a", "b"], ["b", "c"])
        assert out == ["a", "b", "c"]

    def test_coalesce_canonical_wins_when_truthy(self) -> None:
        assert _coalesce("X", "Y") == "X"
        assert _coalesce("", "Y") == "Y"
        assert _coalesce(None, "Y") == "Y"

    def test_max_numeric_higher_wins(self) -> None:
        assert _max_numeric(0.5, 0.8) == 0.8
        assert _max_numeric(0.8, 0.5) == 0.8
        assert _max_numeric(None, 0.5) == 0.5
        assert _max_numeric(0.5, None) == 0.5

    def test_max_date_lex_compare_later_wins(self) -> None:
        assert _max_date("2025-05-01", "2025-04-01") == "2025-05-01"
        assert _max_date("2025-04-01", "2025-05-01") == "2025-05-01"
        assert _max_date("", "2025-05-01") == "2025-05-01"

    def test_merge_field_sources_canonical_wins_per_key(self) -> None:
        cmeta = {"field_sources": {"linkedin_url": "manual:2024-01-01"}}
        ameta = {
            "field_sources": {
                "linkedin_url": "api:apollo:2025-01-01",
                "twitter_url": "manual:2023-01-01",
            }
        }
        merged = {"linkedin_url": "x", "twitter_url": "y"}
        out = _merge_field_sources(cmeta, ameta, merged)
        assert out == {
            "linkedin_url": "manual:2024-01-01",  # canonical wins
            "twitter_url": "manual:2023-01-01",  # absorbed-only carried
        }

    def test_merge_field_sources_prunes_keys_not_in_merged(self) -> None:
        cmeta = {"field_sources": {"current_title": "api:apollo:2025-01-01"}}
        ameta = {"field_sources": {}}
        # current_title is NOT in merged → entry pruned
        merged = {"name": "x"}
        out = _merge_field_sources(cmeta, ameta, merged)
        assert out is None  # pruned everything → None

    def test_merge_field_sources_per_value_survives_list_reorder(self) -> None:
        """Per docs/provenance-shape.md §2.1: per-value attribution is
        co-indexed BY VALUE, not by position. Reordering the underlying
        list must carry attributions with the values."""
        cmeta = {
            "emails": ["a@x.com", "b@y.com"],
            "field_sources": {
                "emails": [
                    {"value": "a@x.com", "source": "api:apollo:2026-04-29"},
                    {"value": "b@y.com", "source": "linkedin:bhandle"},
                ]
            },
        }
        ameta: dict[str, object] = {"field_sources": {}}
        # Merged list reordered relative to canonical's field_sources order.
        merged = {"emails": ["b@y.com", "a@x.com"]}
        out = _merge_field_sources(cmeta, ameta, merged)
        assert out is not None
        assert out["emails"] == [
            {"value": "b@y.com", "source": "linkedin:bhandle"},
            {"value": "a@x.com", "source": "api:apollo:2026-04-29"},
        ]

    def test_merge_field_sources_list_of_dicts_employment_history(self) -> None:
        """Per docs/provenance-shape.md §2.2: per-value attribution
        attaches to the WHOLE dict for list-of-dicts fields like
        ``apollo_employment_history``. After merge both dicts present
        with their respective sources."""
        c_emp = [{"company": "Kromatic", "title": "Founder"}]
        a_emp = [{"company": "SECUDE", "title": "Director"}]
        cmeta = {
            "apollo_employment_history": c_emp,
            "field_sources": {
                "apollo_employment_history": [
                    {"value": c_emp[0], "source": "api:apollo:2026-04-29"},
                ]
            },
        }
        ameta = {
            "apollo_employment_history": a_emp,
            "field_sources": {
                "apollo_employment_history": [
                    {"value": a_emp[0], "source": "linkedin:tristankromer"},
                ]
            },
        }
        # Merged list = canonical first, absorbed second (list-union).
        merged = {"apollo_employment_history": c_emp + a_emp}
        out = _merge_field_sources(cmeta, ameta, merged)
        assert out is not None
        assert out["apollo_employment_history"] == [
            {
                "value": {"company": "Kromatic", "title": "Founder"},
                "source": "api:apollo:2026-04-29",
            },
            {
                "value": {"company": "SECUDE", "title": "Director"},
                "source": "linkedin:tristankromer",
            },
        ]

    def test_merge_field_sources_prunes_stale_per_value_entry(self) -> None:
        """Per docs/provenance-shape.md §2.4: a per-value attribution
        entry whose ``value`` no longer appears in the merged list is
        dropped at write time, mirroring the prune-dangling rule."""
        cmeta = {
            "emails": ["a@x.com"],
            "field_sources": {
                "emails": [
                    {"value": "a@x.com", "source": "api:apollo:2026-04-29"},
                    # Stale — ``b@y.com`` is not in the merged list.
                    {"value": "b@y.com", "source": "linkedin:bhandle"},
                ]
            },
        }
        ameta: dict[str, object] = {"field_sources": {}}
        merged = {"emails": ["a@x.com"]}
        out = _merge_field_sources(cmeta, ameta, merged)
        assert out is not None
        assert out["emails"] == [
            {"value": "a@x.com", "source": "api:apollo:2026-04-29"},
        ]


class TestDedupePerformMerge:
    """End-to-end merge of a duplicate pair — locks every field-class rule."""

    def _setup_pair(self, tmp_path: Path) -> tuple[Path, Path]:
        wiki = _wiki_root(tmp_path)
        canonical = _person_wiki(
            wiki,
            uid="canon01",
            name="Alice",
            extra={
                "emails": ["alice@a.com"],
                "tags": ["client"],
                "aliases": [],
                "warm_score": 5.0,
                "updated": "2025-01-01",
                "apollo_id": "apollo-123",
                "current_title": "VP",
                "linkedin_url": "https://linkedin.com/in/alice",
                "source": "wiki:canonical",
                "field_sources": {
                    "current_title": "manual:2024-01-01",
                },
            },
            body="# Alice\n\nCanonical body.\n",
        )
        absorbed = _person_wiki(
            wiki,
            uid="absorb1",
            name="Alice Smith",  # different name → alias
            extra={
                "emails": ["alice@b.com"],
                "tags": ["fintech"],
                "warm_score": 9.0,
                "updated": "2025-04-01",
                "apollo_id": "apollo-456",  # canonical wins (truthy)
                "current_title": "Director",  # canonical wins (truthy)
                "twitter_url": "https://twitter.com/alice",
                "source": "wiki:absorbed",
                "field_sources": {
                    "twitter_url": "manual:2024-06-01",
                    "current_title": "linkedin:2024-12-01",  # canonical's wins
                },
            },
            body="# Alice Smith\n\nAbsorbed body — bonus context.\n",
        )
        return canonical, absorbed

    def test_perform_merge_list_union_canonical_first(self, tmp_path: Path) -> None:
        cpath, apath = self._setup_pair(tmp_path)
        _perform_merge(cpath, apath, dry_run=False)
        meta, _body = parse_frontmatter(cpath.read_text())
        assert meta["emails"] == ["alice@a.com", "alice@b.com"]
        assert meta["tags"] == ["client", "fintech"]
        # name differs → absorbed.name appended to aliases
        assert "Alice Smith" in meta["aliases"]

    def test_perform_merge_coalesce_canonical_wins_when_truthy(
        self,
        tmp_path: Path,
    ) -> None:
        cpath, apath = self._setup_pair(tmp_path)
        _perform_merge(cpath, apath, dry_run=False)
        meta, _body = parse_frontmatter(cpath.read_text())
        # Apollo namespace + current_title: canonical wins (both truthy).
        assert meta["apollo_id"] == "apollo-123"
        assert meta["current_title"] == "VP"
        # twitter_url is now in _SOCIAL_KEYS coalesce set (#106) — absorbed-only
        # value carries forward to the canonical merged wiki.
        assert meta["twitter_url"] == "https://twitter.com/alice"

    def test_perform_merge_max_numeric_higher_wins(self, tmp_path: Path) -> None:
        cpath, apath = self._setup_pair(tmp_path)
        _perform_merge(cpath, apath, dry_run=False)
        meta, _body = parse_frontmatter(cpath.read_text())
        assert meta["warm_score"] == 9.0  # absorbed's higher

    def test_perform_merge_updated_stamped_today_overrides_max_date(
        self,
        tmp_path: Path,
    ) -> None:
        cpath, apath = self._setup_pair(tmp_path)
        _perform_merge(cpath, apath, dry_run=False)
        meta, _body = parse_frontmatter(cpath.read_text())
        # Even though absorbed's date (2025-04-01) is later, the merge stamps
        # `updated` with today.
        assert meta["updated"] == date.today().isoformat()

    def test_perform_merge_audit_trail_canonical_source_wins(
        self,
        tmp_path: Path,
    ) -> None:
        cpath, apath = self._setup_pair(tmp_path)
        _perform_merge(cpath, apath, dry_run=False)
        meta, _body = parse_frontmatter(cpath.read_text())
        assert meta["source"] == "wiki:canonical"
        assert meta["merged_from"] == ["absorb1"]
        assert meta["merged_from_sources"] == {"absorb1": "wiki:absorbed"}

    def test_perform_merge_field_sources_canonical_wins_per_key(
        self,
        tmp_path: Path,
    ) -> None:
        cpath, apath = self._setup_pair(tmp_path)
        _perform_merge(cpath, apath, dry_run=False)
        meta, _body = parse_frontmatter(cpath.read_text())
        fs = meta["field_sources"]
        # canonical's current_title provenance wins
        assert fs["current_title"] == "manual:2024-01-01"
        # twitter_url provenance gets pruned because the field itself was
        # dropped on merge (see twitter-not-in-coalesce-sets bug above).

    def test_perform_merge_body_appends_absorbed_when_distinct(
        self,
        tmp_path: Path,
    ) -> None:
        cpath, apath = self._setup_pair(tmp_path)
        _perform_merge(cpath, apath, dry_run=False)
        body_text = cpath.read_text()
        assert "## Merged from absorb1" in body_text
        assert "Canonical body" in body_text
        assert "Absorbed body" in body_text

    def test_perform_merge_idempotent_already_merged(self, tmp_path: Path) -> None:
        cpath, apath = self._setup_pair(tmp_path)
        pair = DuplicatePair(
            canonical_uid="canon01",
            absorbed_uid="absorb1",
            match_signal="apollo_id",
            canonical_path=str(cpath),
            absorbed_path=str(apath),
        )
        report1 = merge_duplicate_persons([pair], apply=True)
        assert report1.merged == 1
        # Re-run: absorbed file is gone → already_merged
        report2 = merge_duplicate_persons([pair], apply=True)
        assert report2.already_merged == 1
        assert report2.merged == 0


# ---------------------------------------------------------------------------
# 9. Disjoint temporal validity — sequential states are not conflicts (#324)
# ---------------------------------------------------------------------------
#
# Doc: docs/conflict-resolution.md § 9. Two claims whose validity windows do
# not overlap describe sequential states of the world and cannot contradict.
# Windows use FUTURE dates so neither member is filtered as expired-inactive
# (#308) — the disjointness itself, not staleness, is what these assert.


def _write_validity_am(
    scope: Path,
    filename: str,
    *,
    name: str,
    body: str,
    valid_from: str = "",
    valid_until: str = "",
    source_type: str = "",
    updated: str = "",
) -> Path:
    """Write an auto-memory file carrying optional validity/provenance frontmatter."""
    scope.mkdir(parents=True, exist_ok=True)
    path = scope / filename
    lines = ["---", f"name: {name}", "type: feedback"]
    if valid_from:
        lines.append(f"valid_from: {valid_from}")
    if valid_until:
        lines.append(f"valid_until: {valid_until}")
    if source_type:
        lines.append(f"source_type: {source_type}")
    if updated:
        lines.append(f"updated: {updated}")
    lines.append("---")
    path.write_text("\n".join(lines) + "\n" + body + "\n", encoding="utf-8")
    return path


def _two_member_validity_root(
    tmp_path: Path,
    *,
    a_valid_until: str = "",
    a_valid_from: str = "",
    b_valid_from: str = "",
    b_valid_until: str = "",
) -> Path:
    """A knowledge root with one 2-member cluster whose members carry windows."""
    knowledge_root = tmp_path / "knowledge"
    scope_name = "-Users-tristankromer-Code"
    scope = knowledge_root / "raw" / "auto-memory" / scope_name
    _write_validity_am(
        scope,
        "feedback_pricing_early.md",
        name="Pricing early",
        body="Our price is $50 per month.",
        valid_from=a_valid_from,
        valid_until=a_valid_until,
    )
    _write_validity_am(
        scope,
        "feedback_pricing_later.md",
        name="Pricing later",
        body="Our price is $70 per month.",
        valid_from=b_valid_from,
        valid_until=b_valid_until,
    )
    out = knowledge_root / "raw" / "_librarian-clusters.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "cluster_id": "pricing-0001",
                "member_paths": [
                    f"{scope_name}/feedback_pricing_early.md",
                    f"{scope_name}/feedback_pricing_later.md",
                ],
                "centroid_score": 0.62,
                "rationale": "cosine >= 0.55; shares tokens: price, per, month",
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    (knowledge_root / "athenaeum.yaml").write_text(
        "recall:\n  extra_intake_roots:\n    - raw/auto-memory\n",
        encoding="utf-8",
    )
    return knowledge_root


def _three_member_validity_root(tmp_path: Path) -> Path:
    """A knowledge root with one 3-member cluster: a disjoint A/B pair plus a
    C with an open window overlapping both. The cluster is NOT pairwise-disjoint
    so the pre-filter does NOT fire and the detector RUNS — exercising the
    Part-2 post-detection guard (a flagged disjoint pair is downgraded)."""
    knowledge_root = tmp_path / "knowledge"
    scope_name = "-Users-tristankromer-Code"
    scope = knowledge_root / "raw" / "auto-memory" / scope_name
    _write_validity_am(
        scope,
        "feedback_pricing_early.md",
        name="Pricing early",
        body="Our price is $50 per month.",
        valid_until="2027-03-31",
    )
    _write_validity_am(
        scope,
        "feedback_pricing_later.md",
        name="Pricing later",
        body="Our price is $70 per month.",
        valid_from="2027-04-01",
    )
    _write_validity_am(
        scope,
        "feedback_pricing_note.md",
        name="Pricing note",
        body="Pricing is reviewed each quarter.",
    )
    out = knowledge_root / "raw" / "_librarian-clusters.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "cluster_id": "pricing-0001",
                "member_paths": [
                    f"{scope_name}/feedback_pricing_early.md",
                    f"{scope_name}/feedback_pricing_later.md",
                    f"{scope_name}/feedback_pricing_note.md",
                ],
                "centroid_score": 0.62,
                "rationale": "cosine >= 0.55; shares tokens: price, per, month",
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    (knowledge_root / "athenaeum.yaml").write_text(
        "recall:\n  extra_intake_roots:\n    - raw/auto-memory\n",
        encoding="utf-8",
    )
    return knowledge_root


class TestValidityWindowsDisjointPredicate:
    """Part 0: the shared ``validity_windows_disjoint`` predicate (#324)."""

    def test_closed_upper_before_open_lower_is_disjoint(self) -> None:
        a = {"valid_until": "2026-03-31"}
        b = {"valid_from": "2026-04-01"}
        assert validity_windows_disjoint(a, b) is True

    def test_symmetric_ordering_is_disjoint(self) -> None:
        # b ends before a begins — the symmetric branch.
        a = {"valid_from": "2026-04-01"}
        b = {"valid_until": "2026-03-31"}
        assert validity_windows_disjoint(a, b) is True

    def test_touching_boundary_shares_day_not_disjoint(self) -> None:
        # valid_until is the INCLUSIVE last-valid day, so a window ending
        # 2026-04-01 and one starting 2026-04-01 share that day.
        a = {"valid_until": "2026-04-01"}
        b = {"valid_from": "2026-04-01"}
        assert validity_windows_disjoint(a, b) is False

    def test_both_open_windows_overlap_not_disjoint(self) -> None:
        assert validity_windows_disjoint({}, {}) is False

    def test_malformed_both_fail_open_not_disjoint(self) -> None:
        # parse_* coerces garbage to None (open) — never disjoint.
        a = {"valid_until": "not-a-date"}
        b = {"valid_from": "garbage"}
        assert validity_windows_disjoint(a, b) is False

    def test_open_upper_bound_overlaps(self) -> None:
        # a has only a lower bound (open upper) → cannot end before b begins.
        a = {"valid_from": "2026-01-01"}
        b = {"valid_from": "2026-06-01"}
        assert validity_windows_disjoint(a, b) is False


class TestDisjointValidityDetectorShortCircuit:
    """Parts 1 & 2: merge.py pre-filter and post-guard (#324)."""

    def test_disjoint_two_member_cluster_skips_detector(
        self,
        tmp_path: Path,
        caplog,
    ) -> None:
        """Case (a): A.valid_until < B.valid_from → NO detector LLM call, no
        pending question, and a logged ``disjoint-validity`` rationale."""
        root = _two_member_validity_root(
            tmp_path,
            a_valid_until="2026-08-31",
            b_valid_from="2026-09-01",
        )
        fake_client = MagicMock()
        with caplog.at_level(logging.INFO, logger="athenaeum.merge"):
            entries = merge_clusters_to_wiki(root, client=fake_client)
        # No detector call at all.
        fake_client.messages.create.assert_not_called()
        assert len(entries) == 1
        assert entries[0].contradictions_detected is False
        assert entries[0].contradiction is not None
        assert entries[0].contradiction.rationale == "disjoint-validity"
        wiki = root / "wiki"
        # No escalation side-effect.
        assert not (wiki / "_pending_questions.md").exists()
        meta, _ = parse_frontmatter(
            next(wiki.glob(f"{AUTO_WIKI_PREFIX}*.md")).read_text(encoding="utf-8")
        )
        assert meta["contradictions_detected"] is False
        assert "status" not in meta
        # Rationale is logged like the declared-pair short-circuit.
        assert any("disjoint-validity" in rec.message for rec in caplog.records)

    def test_overlapping_windows_still_run_detector(
        self,
        tmp_path: Path,
    ) -> None:
        """Case (b): overlapping windows behave exactly as today — the
        detector runs (``messages.create`` IS called)."""
        root = _two_member_validity_root(
            tmp_path,
            a_valid_until="2026-09-30",
            b_valid_from="2026-08-01",
        )
        fake_client = MagicMock()
        response = MagicMock()
        response.content = [
            MagicMock(
                text=(
                    '{"detected": false, "conflict_type": null, '
                    '"members_involved": [], "conflicting_passages": [], '
                    '"rationale": "same price restated"}'
                )
            )
        ]
        fake_client.messages.create.return_value = response
        entries = merge_clusters_to_wiki(root, client=fake_client)
        fake_client.messages.create.assert_called()
        assert len(entries) == 1
        assert entries[0].contradictions_detected is False

    def test_open_windows_still_run_detector(
        self,
        tmp_path: Path,
    ) -> None:
        """Both windows absent (open) → overlap by default → detector runs."""
        root = _two_member_validity_root(tmp_path)  # no bounds on either side
        fake_client = MagicMock()
        response = MagicMock()
        response.content = [
            MagicMock(
                text=(
                    '{"detected": false, "conflict_type": null, '
                    '"members_involved": [], "conflicting_passages": [], '
                    '"rationale": "no conflict"}'
                )
            )
        ]
        fake_client.messages.create.return_value = response
        merge_clusters_to_wiki(root, client=fake_client)
        fake_client.messages.create.assert_called()

    def test_three_member_cluster_downgrades_detected_disjoint_pair(
        self,
        tmp_path: Path,
        caplog,
    ) -> None:
        """Part 2: a cluster with an overlapping pair still reaches the detector;
        when the detector flags the DISJOINT pair, the post-guard downgrades to
        not-detected (rationale ``disjoint-validity``) and writes no pending
        question."""
        root = _three_member_validity_root(tmp_path)
        scope_name = "-Users-tristankromer-Code"
        fake_client = MagicMock()
        response = MagicMock()
        response.content = [
            MagicMock(
                text=json.dumps(
                    {
                        "detected": True,
                        "conflict_type": "factual",
                        "members_involved": [
                            f"{scope_name}/feedback_pricing_early.md",
                            f"{scope_name}/feedback_pricing_later.md",
                        ],
                        "conflicting_passages": [
                            "$50 per month",
                            "$70 per month",
                        ],
                        "rationale": "price differs",
                    }
                )
            )
        ]
        fake_client.messages.create.return_value = response
        with caplog.at_level(logging.INFO, logger="athenaeum.merge"):
            entries = merge_clusters_to_wiki(root, client=fake_client)
        # Detector DID run (cluster is not pairwise-disjoint)...
        fake_client.messages.create.assert_called()
        # ...but the flagged pair is disjoint, so the entry is downgraded and
        # no pending question is written.
        assert len(entries) == 1
        assert entries[0].contradictions_detected is False
        assert entries[0].contradiction is not None
        assert entries[0].contradiction.rationale == "disjoint-validity"
        assert not (root / "wiki" / "_pending_questions.md").exists()
        assert any("disjoint-validity" in rec.message for rec in caplog.records)


class TestDisjointValidityResolverSynthetic:
    """Part 3: resolutions._disjoint_validity_verdict (#324)."""

    def _am(
        self,
        tmp_path: Path,
        filename: str,
        *,
        valid_from: str = "",
        valid_until: str = "",
    ) -> AutoMemoryFile:
        path = tmp_path / filename
        path.write_text("---\nname: x\ntype: feedback\n---\nbody\n", encoding="utf-8")
        return AutoMemoryFile(
            path=path,
            origin_scope="scope-x",
            memory_type="feedback",
            name=filename.replace(".md", ""),
            valid_from=valid_from,
            valid_until=valid_until,
        )

    def test_disjoint_pair_resolves_not_a_conflict_without_llm(
        self,
        tmp_path: Path,
    ) -> None:
        a = self._am(tmp_path, "a.md", valid_until="2026-03-31")
        b = self._am(tmp_path, "b.md", valid_from="2026-04-01")
        detector = ContradictionResult(
            detected=True,
            conflict_type="factual",
            members_involved=["scope-x/a.md", "scope-x/b.md"],
            conflicting_passages=["price $50", "price $70"],
            rationale="stated different prices",
        )
        fake_client = MagicMock()
        proposal = propose_resolution(detector, [a, b], fake_client)
        # No Opus call — the synthetic verdict short-circuits.
        fake_client.messages.create.assert_not_called()
        assert proposal.action == "not_a_conflict"
        assert proposal.recommended_winner == "neither"
        assert proposal.confidence == 1.0

    def test_overlapping_pair_falls_through_to_llm(
        self,
        tmp_path: Path,
    ) -> None:
        a = self._am(tmp_path, "a.md", valid_until="2026-09-30")
        b = self._am(tmp_path, "b.md", valid_from="2026-08-01")
        detector = ContradictionResult(
            detected=True,
            conflict_type="factual",
            members_involved=["scope-x/a.md", "scope-x/b.md"],
            conflicting_passages=["price $50", "price $70"],
            rationale="stated different prices",
        )
        fake_client = MagicMock()
        fake_client.messages.create.return_value = MagicMock(
            content=[
                MagicMock(
                    text=(
                        '{"recommended_winner": "a", "action": "keep_a", '
                        '"confidence": 0.8, "rationale": "newer", '
                        '"source_precedence_used": []}'
                    )
                )
            ]
        )
        proposal = propose_resolution(detector, [a, b], fake_client)
        # Overlapping → the LLM path runs.
        fake_client.messages.create.assert_called_once()
        assert proposal.action == "keep_a"


class TestScopeHeaderRendering:
    """Part 4: contradictions._member_scope_header / _build_user_message (#324)."""

    def _am_on_disk(
        self,
        tmp_path: Path,
        filename: str,
        *,
        valid_from: str = "",
        valid_until: str = "",
        source_type: str = "",
        updated: str = "",
    ) -> AutoMemoryFile:
        path = _write_validity_am(
            tmp_path,
            filename,
            name="probe",
            body="Some memory body text.",
            valid_from=valid_from,
            valid_until=valid_until,
            source_type=source_type,
            updated=updated,
        )
        return AutoMemoryFile(
            path=path,
            origin_scope="scope-x",
            memory_type="feedback",
            name="probe",
        )

    def test_full_header_renders_all_segments(self, tmp_path: Path) -> None:
        am = self._am_on_disk(
            tmp_path,
            "m.md",
            valid_from="2026-04-01",
            valid_until="2026-06-30",
            source_type="user-stated",
            updated="2026-06-30",
        )
        header = _member_scope_header(am)
        assert header == (
            "valid: 2026-04-01 → 2026-06-30 · source: user-stated · updated: 2026-06-30"
        )

    def test_open_upper_bound_uses_open_token(self, tmp_path: Path) -> None:
        am = self._am_on_disk(tmp_path, "m.md", valid_from="2026-04-01")
        assert _member_scope_header(am) == "valid: 2026-04-01 → open"

    def test_no_metadata_yields_empty_header(self, tmp_path: Path) -> None:
        # Default source_type (inferred) + no window + no updated → no line.
        am = self._am_on_disk(tmp_path, "m.md")
        assert _member_scope_header(am) == ""

    def test_scope_line_is_outside_memory_block(self, tmp_path: Path) -> None:
        am = self._am_on_disk(
            tmp_path,
            "m.md",
            valid_from="2026-04-01",
            valid_until="2026-06-30",
        )
        msg = _build_user_message([am])
        lines = msg.splitlines()
        scope_idx = next(i for i, ln in enumerate(lines) if ln.startswith("scope:"))
        memory_idx = next(i for i, ln in enumerate(lines) if ln == "<memory>")
        # The trusted scope header precedes the untrusted <memory> block.
        assert scope_idx < memory_idx
        assert "valid: 2026-04-01 → 2026-06-30" in lines[scope_idx]

    def test_default_source_and_no_window_adds_no_scope_line(
        self,
        tmp_path: Path,
    ) -> None:
        am = self._am_on_disk(tmp_path, "m.md")
        msg = _build_user_message([am])
        assert "scope:" not in msg


# ---------------------------------------------------------------------------
# #308 slice 2 — resolver interval-close on temporal supersession
# ---------------------------------------------------------------------------
#
# `enact_resolution` stamps the LOSER's `valid_until` in ADDITION to the
# existing supersession mark when a resolution establishes a TEMPORAL
# supersession. Arithmetic: `loser.valid_until = winner.valid_from` (inclusive
# last-valid date, same boundary day — see the BOUNDARY RECONCILIATION note in
# resolutions.py: this makes the pair non-disjoint at the boundary by design,
# acceptable because the loser is also `superseded_by` and hence inactive).
# Winner without a `valid_from` falls back to the resolution date (today).
# Only ever CLOSES, never widens. These tests pin the EXACT stamped value.


def _iclose_member(
    path: Path,
    name: str,
    *,
    valid_from: str | None = None,
    valid_until: str | None = None,
    created: str | None = None,
    body: str = "the claim",
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = [f"name: {name}", "type: feedback"]
    if valid_from is not None:
        fm.append(f"valid_from: {valid_from}")
    if valid_until is not None:
        fm.append(f"valid_until: {valid_until}")
    if created is not None:
        fm.append(f"created: {created}")
    path.write_text(
        "---\n" + "\n".join(fm) + "\n---\n\n" + body + "\n",
        encoding="utf-8",
    )
    return path


def _iclose_proposal(action: str) -> ResolutionProposal:
    winner = {"keep_a": "a", "keep_b": "b"}.get(action, "neither")
    return ResolutionProposal(
        recommended_winner=winner,  # type: ignore[arg-type]
        action=action,  # type: ignore[arg-type]
        rationale=f"test-{action}",
        confidence=1.0,
        source_precedence_used=["a:user > b:unsourced"],
    )


class TestIntervalCloseSlice2:
    def test_keep_a_closes_loser_b_to_winner_valid_from(self, tmp_path: Path) -> None:
        a = _iclose_member(tmp_path / "a.md", "Winner A", valid_from="2026-06-01")
        b = _iclose_member(tmp_path / "b.md", "Loser B")
        ret = enact_resolution(_iclose_proposal("keep_a"), [a, b])
        assert ret == b
        meta_b, _ = parse_frontmatter(b.read_text(encoding="utf-8"))
        # Interval closed at the winner's valid_from, EXACT value.
        assert meta_b["valid_until"] == "2026-06-01"
        # Interval-close AUGMENTS, does not replace, the supersession mark.
        assert meta_b["superseded_by"] == "Winner A"
        # Winner untouched; nothing deleted.
        meta_a, _ = parse_frontmatter(a.read_text(encoding="utf-8"))
        assert "valid_until" not in meta_a
        assert a.exists() and b.exists()

    def test_keep_b_closes_loser_a_to_winner_valid_from(self, tmp_path: Path) -> None:
        a = _iclose_member(tmp_path / "a.md", "Loser A")
        b = _iclose_member(tmp_path / "b.md", "Winner B", valid_from="2026-07-15")
        ret = enact_resolution(_iclose_proposal("keep_b"), [a, b])
        assert ret == a
        meta_a, _ = parse_frontmatter(a.read_text(encoding="utf-8"))
        assert meta_a["valid_until"] == "2026-07-15"
        assert meta_a["superseded_by"] == "Winner B"

    def test_keep_a_winner_without_valid_from_falls_back_to_today(
        self, tmp_path: Path
    ) -> None:
        a = _iclose_member(tmp_path / "a.md", "Winner A")  # no valid_from
        b = _iclose_member(tmp_path / "b.md", "Loser B")
        enact_resolution(_iclose_proposal("keep_a"), [a, b])
        meta_b, _ = parse_frontmatter(b.read_text(encoding="utf-8"))
        assert meta_b["valid_until"] == date.today().isoformat()
        assert meta_b["superseded_by"] == "Winner A"

    def test_keep_a_never_widens_existing_tighter_bound(self, tmp_path: Path) -> None:
        # Loser already ends 2026-01-01; winner's valid_from is LATER. A
        # resolution must not EXTEND validity — keep the earlier bound.
        a = _iclose_member(tmp_path / "a.md", "Winner A", valid_from="2026-06-01")
        b = _iclose_member(tmp_path / "b.md", "Loser B", valid_until="2026-01-01")
        enact_resolution(_iclose_proposal("keep_a"), [a, b])
        meta_b, _ = parse_frontmatter(b.read_text(encoding="utf-8"))
        assert meta_b["valid_until"] == "2026-01-01"

    def test_keep_a_widens_nothing_but_tightens_looser_bound(
        self, tmp_path: Path
    ) -> None:
        # Loser ends 2026-12-31 but the winner took over 2026-06-01 — the close
        # TIGHTENS to the earlier winner boundary.
        a = _iclose_member(tmp_path / "a.md", "Winner A", valid_from="2026-06-01")
        b = _iclose_member(tmp_path / "b.md", "Loser B", valid_until="2026-12-31")
        enact_resolution(_iclose_proposal("keep_a"), [a, b])
        meta_b, _ = parse_frontmatter(b.read_text(encoding="utf-8"))
        assert meta_b["valid_until"] == "2026-06-01"

    def test_not_a_conflict_snapshot_closes_older_by_valid_from(
        self, tmp_path: Path
    ) -> None:
        older = _iclose_member(tmp_path / "a.md", "Old Snap", valid_from="2026-01-01")
        newer = _iclose_member(tmp_path / "b.md", "New Snap", valid_from="2026-05-01")
        ret = enact_resolution(_iclose_proposal("not_a_conflict"), [older, newer])
        assert ret == older
        meta_old, _ = parse_frontmatter(older.read_text(encoding="utf-8"))
        meta_new, _ = parse_frontmatter(newer.read_text(encoding="utf-8"))
        # OLDER closes at the NEWER's lower bound; newer is untouched.
        assert meta_old["valid_until"] == "2026-05-01"
        assert "valid_until" not in meta_new

    def test_not_a_conflict_snapshot_orders_by_ingestion_when_no_valid_from(
        self, tmp_path: Path
    ) -> None:
        # No valid_from on either side → order by created (ingestion); boundary is the
        # newer's ingestion date (it has no valid_from either).
        a = _iclose_member(tmp_path / "a.md", "First", created="2026-02-10")
        b = _iclose_member(tmp_path / "b.md", "Second", created="2026-04-20")
        ret = enact_resolution(_iclose_proposal("not_a_conflict"), [a, b])
        assert ret == a
        meta_a, _ = parse_frontmatter(a.read_text(encoding="utf-8"))
        assert meta_a["valid_until"] == "2026-04-20"

    def test_not_a_conflict_no_ordering_signal_does_not_stamp(
        self, tmp_path: Path
    ) -> None:
        # No valid_from, no ingestion date → no reliable ordering → no stamp.
        a = _iclose_member(tmp_path / "a.md", "A")
        b = _iclose_member(tmp_path / "b.md", "B")
        assert enact_resolution(_iclose_proposal("not_a_conflict"), [a, b]) is None
        meta_a, _ = parse_frontmatter(a.read_text(encoding="utf-8"))
        meta_b, _ = parse_frontmatter(b.read_text(encoding="utf-8"))
        assert "valid_until" not in meta_a
        assert "valid_until" not in meta_b

    @pytest.mark.parametrize(
        "action",
        [
            "correct_a",
            "correct_b",
            "forget_a",
            "forget_b",
            "deprecate_both",
            "retain_both_with_context",
            "merge",
            "propose_merge",
        ],
    )
    def test_non_supersession_actions_do_not_stamp_valid_until(
        self, action: str, tmp_path: Path
    ) -> None:
        a = _iclose_member(tmp_path / "a.md", "A", valid_from="2026-06-01")
        b = _iclose_member(tmp_path / "b.md", "B", valid_from="2026-06-01")
        enact_resolution(_iclose_proposal(action), [a, b])
        # Whichever files survive must NOT have gained a valid_until: these
        # verdicts are WRONG-claim / both-stale, not valid-then-replaced.
        for p in (a, b):
            if p.exists():
                meta, _ = parse_frontmatter(p.read_text(encoding="utf-8"))
                assert "valid_until" not in meta

    def test_stamped_value_reparses_as_the_pinned_date(self, tmp_path: Path) -> None:
        # Lock the round-trip: the stored string parses back to the exact date.
        a = _iclose_member(tmp_path / "a.md", "Winner A", valid_from="2026-06-01")
        b = _iclose_member(tmp_path / "b.md", "Loser B")
        enact_resolution(_iclose_proposal("keep_a"), [a, b])
        meta_b, _ = parse_frontmatter(b.read_text(encoding="utf-8"))
        assert parse_valid_until(meta_b) == date(2026, 6, 1)
