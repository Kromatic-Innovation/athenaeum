# SPDX-License-Identifier: Apache-2.0
"""athenaeum#1196 — foreign-shaped agent memories must normalize into the
declared type vocabulary.

AC1 (the guarantee test): feeds foreign-shaped raw intake through the REAL
pipeline (:func:`athenaeum.librarian.process_one` — Tier 0 -> Tier 1 -> Tier
2 -> Tier 3, exactly the entry point the nightly entity loop calls per raw
file) with a canned-response :class:`~tests.conftest.FakeLLMClient` standing
in for the live Anthropic call, and asserts every resulting wiki page has a
``type`` in declared ∪ ``KNOWN_TYPES`` and carries entity frontmatter. The
LLM responses are stubbed; the parsing/clamping code that turns them into a
:class:`~athenaeum.models.WikiEntity` (``tiers.parse_tier2_entities``,
``tiers.tier3_entity_from_text`` / ``tiers.tier3_merge``) runs for real.

Two inputs, per the issue's AC:
  (a) the raw Claude Code memory shape quoted in athenaeum#1196 — ``name``/
      ``description`` top-level, ``metadata.node_type: memory``,
      ``metadata.type: feedback`` NESTED (not top-level), no ``uid``, no
      ``field_sources``.
  (b) an arbitrary-YAML file carrying a novel top-level ``type:`` that is in
      neither the declared types nor ``KNOWN_TYPES``.

A THIRD scenario below additionally drives a genuine Tier-3 MERGE (not a
create) from a foreign-shaped raw file, because athenaeum's own, tested
provenance contract (see ``tests/test_tiers.py::TestTier3Provenance``,
issue athenaeum#95) stamps ``source:`` on a freshly-CREATED page and
``field_sources:`` only on a page a merge actually touches — a brand-new
page has nothing yet to attribute per-field, so it legitimately carries
``source`` rather than ``field_sources``. That merge scenario is also the
live regression test backing AC2's finding below: merging never changes an
existing page's ``type`` at all (see ``TestAC2MergeCannotIntroduceForeignType``).

AC2 (investigate the merge-created-a-page hypothesis): the issue comment
narrowed the athenaeum#1196 corpus finding to a single stray page
(``type: issue``) attributed to ``tier3-merge`` during an interrupted run,
and asked candidate (1) to be checked in code: does a merge targeting a
MISSING page fall back to creating one, bypassing the ``valid_types``
clamp? ``TestAC2MergeCannotIntroduceForeignType`` answers this directly
against today's source, with citations in each test's docstring.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

from athenaeum.entity_schema import declared_entity_classes
from athenaeum.librarian import _apply_tier3_results, process_one
from athenaeum.models import (
    EntityAction,
    EntityIndex,
    ProcessingResult,
    RawFile,
    WikiEntity,
    parse_frontmatter,
)
from athenaeum.schemas import KNOWN_TYPES
from athenaeum.tiers import tier3_derive_actions
from athenaeum.wiki_write_guard import (
    TYPE_REJECTED_DIR_NAME,
    list_type_rejected,
    resolve_admitted_wiki_types,
)
from tests.conftest import FakeLLMClient

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_DECLARED_TYPES = ["person", "company", "project", "reference"]


def _wiki_root(tmp_path: Path) -> Path:
    wiki_root = tmp_path / "wiki"
    (wiki_root / "_schema").mkdir(parents=True)
    rows = "\n".join(f"| {t} |" for t in _DECLARED_TYPES)
    (wiki_root / "_schema" / "types.md").write_text(
        f"# Types\n\n| Type |\n|------|\n{rows}\n"
    )
    return wiki_root


def _raw(content: str, name: str = "20260807T090000Z-aabb0011.md") -> RawFile:
    return RawFile(
        path=Path(f"/tmp/fake/raw/claude-session/{name}"),
        source="claude-session",
        timestamp="20260807T090000Z",
        uuid8="aabb0011",
        _content=content,
    )


# (a) The raw Claude Code memory shape quoted in athenaeum#1196.
CLAUDE_CODE_MEMORY_SHAPE = textwrap.dedent(
    """\
    ---
    name: feedback-push-committed-code
    description: Always push committed code to origin, never leave it staged only locally.
    metadata:
      node_type: memory
      type: feedback
      originSessionId: fa4a4598-0000-0000-0000-000000000000
    ---

    Always push committed code to origin, never leave it staged only locally.
    """
)

# (b) Arbitrary YAML with a novel top-level `type:` in neither declared
# types nor KNOWN_TYPES.
ARBITRARY_YAML_NOVEL_TYPE = textwrap.dedent(
    """\
    ---
    type: shibboleth
    name: A record shape nobody declared
    ---

    Some agent invented its own top-level type on the way in.
    """
)


def _tier2_response(entity_type: str, name: str, observations: str) -> str:
    import json

    return json.dumps(
        [
            {
                "name": name,
                "entity_type": entity_type,
                "tags": [],
                "access": "internal",
                "observations": observations,
            }
        ]
    )


# ---------------------------------------------------------------------------
# AC1 — the guarantee test
# ---------------------------------------------------------------------------


class TestAC1ForeignShapedIntakeNormalizes:
    def test_claude_code_memory_shape_normalizes_via_real_pipeline(
        self, tmp_path: Path
    ) -> None:
        """Input (a). tier0_passthrough must reject this shape (no top-level
        uid/type -- only 'name' is top-level, 'type' is nested under
        metadata) and fall through to Tier 1/2/3, which must land a properly
        typed, properly shaped entity page."""
        wiki_root = _wiki_root(tmp_path)
        raw = _raw(CLAUDE_CODE_MEMORY_SHAPE)
        index = EntityIndex(wiki_root)

        # The classify (Tier 2) response deliberately proposes the SAME
        # foreign type the raw file itself declared ("feedback") -- feedback
        # was folded out of KNOWN_TYPES (schemas.py, issue athenaeum#970) and is
        # NOT in this wiki's declared types either, so parse_tier2_entities's
        # REAL clamp (entity_type not in valid_types -> "reference") must be
        # what saves this, not a mock of it.
        classify_client = FakeLLMClient(
            text=_tier2_response(
                "feedback",
                "Push committed code to origin",
                "Always push committed code to origin, never leave it staged only locally.",
            )
        )
        write_client = FakeLLMClient(
            text="# Push committed code to origin\n\n"
            "Always push committed code to origin, never leave it staged only locally.\n"
        )

        result = process_one(
            raw,
            index,
            wiki_root,
            classify_client,
            valid_types=_DECLARED_TYPES,
            valid_tags=[],
            valid_access=["open", "internal", "confidential", "personal"],
            write_client=write_client,
        )

        assert result.created, "expected the foreign-shaped memory to land a wiki page"
        assert result.type_rejected == 0
        admitted = resolve_admitted_wiki_types(wiki_root)
        for entity in result.created:
            assert entity.type in admitted, (
                f"page type {entity.type!r} escaped declared ∪ KNOWN_TYPES"
            )
            # "feedback" must not have sprawled through -- it was clamped.
            assert entity.type != "feedback"
            assert entity.uid, "entity page must carry a non-empty uid"
            assert entity.name, "entity page must carry a non-empty name"

        # Prove it on the actual bytes written to wiki/, not just the
        # in-memory WikiEntity.
        page_path = wiki_root / result.created[0].filename
        assert page_path.exists()
        meta, _body = parse_frontmatter(page_path.read_text(encoding="utf-8"))
        assert meta["type"] in admitted
        assert meta["uid"]
        assert meta["name"]
        # A freshly CREATED page's provenance is `source:` (issue athenaeum#95,
        # tests/test_tiers.py::TestTier3Provenance) -- field_sources is
        # stamped only on a page a MERGE actually touches (see
        # TestAC1MergeAlsoNormalizesAndCarriesFieldSources below for that
        # half of the "field_sources" wording in the AC).
        assert meta.get("source", "").startswith("claude:tier3-create:")

    def test_arbitrary_yaml_novel_type_normalizes_via_real_pipeline(
        self, tmp_path: Path
    ) -> None:
        """Input (b). A completely novel type nobody declared must still be
        clamped by the REAL parse_tier2_entities code, not a mock of it."""
        wiki_root = _wiki_root(tmp_path)
        raw = _raw(
            ARBITRARY_YAML_NOVEL_TYPE, name="20260808T090000Z-ccdd2222.md"
        )
        index = EntityIndex(wiki_root)

        classify_client = FakeLLMClient(
            text=_tier2_response(
                "shibboleth",
                "A record shape nobody declared",
                "Some agent invented its own top-level type on the way in.",
            )
        )
        write_client = FakeLLMClient(
            text="# A record shape nobody declared\n\n"
            "Some agent invented its own top-level type on the way in.\n"
        )

        result = process_one(
            raw,
            index,
            wiki_root,
            classify_client,
            valid_types=_DECLARED_TYPES,
            valid_tags=[],
            valid_access=["open", "internal", "confidential", "personal"],
            write_client=write_client,
        )

        assert result.created
        assert result.type_rejected == 0
        admitted = resolve_admitted_wiki_types(wiki_root)
        for entity in result.created:
            assert entity.type in admitted
            assert entity.type != "shibboleth"
            assert entity.uid
            assert entity.name


class TestAC1MergeAlsoNormalizesAndCarriesFieldSources:
    def test_foreign_shaped_note_merges_into_existing_entity_with_field_sources(
        self, tmp_path: Path
    ) -> None:
        """A foreign-shaped raw file that MENTIONS an existing entity by name
        drives Tier 1 to dispatch a real Tier-3 merge (not a create). This
        is the write-kind that actually stamps `field_sources:` (issue
        athenaeum#95's documented, tested contract) -- and, as a bonus, is a
        live proof for AC2: the merge must not touch `type` at all (see
        stamp_merge_provenance, tiers.py, which stamps only `updated` and
        `field_sources`)."""
        wiki_root = _wiki_root(tmp_path)
        (wiki_root / "a1b2c3d4-acme-corp.md").write_text(
            textwrap.dedent(
                """\
                ---
                uid: a1b2c3d4
                type: company
                name: Acme Corp
                access: internal
                created: '2026-01-01'
                updated: '2026-01-01'
                source: api:apollo
                ---

                # Acme Corp

                Fintech startup.
                """
            ),
            encoding="utf-8",
        )
        index = EntityIndex(wiki_root)

        # Foreign-shaped raw (Claude Code memory shape) that happens to
        # mention an existing entity by name -- Tier 1's programmatic match
        # (no LLM) finds "Acme Corp" and dispatches an update action; Tier 2
        # classification is irrelevant to this action (Tier 1 matches are
        # independent of Tier 2's own entity list), so the classify stub
        # returns no NEW entities at all.
        raw = _raw(
            textwrap.dedent(
                """\
                ---
                name: acme-corp-follow-up
                description: Note about Acme Corp's engagement.
                metadata:
                  node_type: memory
                  type: feedback
                ---

                Acme Corp expanded operations into a new region this quarter.
                """
            ),
            name="20260809T090000Z-eeff3333.md",
        )

        classify_client = FakeLLMClient(text="[]")
        # tier3_merge's patch-mode attempt gets this same plain-prose text on
        # its first call, which parse_merge_ops_response cannot read as a
        # valid ops list -- it falls back to the full-echo path
        # automatically, which re-calls the SAME client and gets this text
        # again, which parse_tier3_merge takes as the new page body verbatim.
        write_client = FakeLLMClient(
            text="# Acme Corp\n\nFintech startup. Expanded ops into a new region.\n"
        )

        result = process_one(
            raw,
            index,
            wiki_root,
            classify_client,
            valid_types=_DECLARED_TYPES,
            valid_tags=[],
            valid_access=["open", "internal", "confidential", "personal"],
            write_client=write_client,
        )

        assert "a1b2c3d4" in result.updated
        assert not result.created  # no NEW page -- merged into the existing one

        page_path = wiki_root / "a1b2c3d4-acme-corp.md"
        meta, body = parse_frontmatter(page_path.read_text(encoding="utf-8"))
        admitted = resolve_admitted_wiki_types(wiki_root)
        # The literal AC1 wording: uid, name, field_sources.
        assert meta["uid"] == "a1b2c3d4"
        assert meta["name"] == "Acme Corp"
        assert isinstance(meta.get("field_sources"), dict)
        assert meta["field_sources"]["body"].startswith("claude:tier3-merge:")
        # Type is unchanged by the merge and stays admitted.
        assert meta["type"] == "company"
        assert meta["type"] in admitted
        assert "new region" in body


# ---------------------------------------------------------------------------
# AC2 — the merge-created-a-page hypothesis, checked against today's source.
# ---------------------------------------------------------------------------


class TestAC2MergeCannotIntroduceForeignType:
    """Issue athenaeum#1196's comment narrows the corpus finding to a single
    `type: issue` page attributed to `tier3-merge`, added (not updated)
    during an interrupted run, and asks candidate (1) to be checked in
    source: does a merge whose target page is MISSING fall back to
    creating one, bypassing the valid_types clamp?

    Traced in both the sync path (`tiers.tier3_derive_actions`, the "update"
    branch) and the batch path (`batch.py`'s phase-2 assembly AND its
    finalize `sync_merges` loop) -- ALL THREE sites read:

        existing_path = index.get_by_uid(action.existing_uid)
        if not existing_path or not existing_path.exists():
            log.warning(...)
            continue

    (tiers.py, ~line 2758 in `tier3_derive_actions`; batch.py, ~line 1524 in
    the phase-2 assembly loop; batch.py, ~line 1786 in the finalize
    `sync_merges` loop.) A missing target is SKIPPED, never created. This
    test proves the sync path's behaviour directly against today's code:
    `tier3_derive_actions` given an "update" action whose `existing_uid` is
    not in the index writes NOTHING and creates NO page.

    Independently, and more strongly: `tier3_merge`/`tier3_merge_full`
    (tiers.py) return only `(updated_body, escalation)` -- a body string,
    never a type -- and every caller (`tier3_derive_actions` here;
    `batch.py`'s `st.merge_ids` and `sync_merges` loops) applies
    `stamp_merge_provenance(meta, ...)` to the EXISTING page's OWN
    `parse_frontmatter`-read meta dict, which stamps only `updated` and
    `field_sources` (see `stamp_merge_provenance`'s body). So even setting
    the missing-page question aside, no code path today lets a merge write
    a `type` at all, let alone an unclamped one -- candidate (1) is refuted
    on both grounds. Candidates (2) (a page committed mid-construction
    during the interrupted run, before frontmatter validation would have
    run) and (3) (`source:` recording the last writer of the body while
    some OTHER path stamped `type: issue`) are about historical `~/knowledge`
    git state this container cannot read, per the issue's own note, and are
    not adjudicated here.
    """

    def test_merge_targeting_missing_page_is_skipped_not_created(
        self, tmp_path: Path
    ) -> None:
        wiki_root = _wiki_root(tmp_path)
        wiki_root.mkdir(exist_ok=True)
        index = EntityIndex(wiki_root)  # empty -- no existing pages at all
        raw = _raw("Some note mentioning a page that does not exist.")

        actions = [
            EntityAction(
                kind="update",
                name="Ghost Page",
                entity_type="",
                tags=[],
                access="",
                existing_uid="deadbeef",  # not in the (empty) index
                observations="irrelevant",
            )
        ]
        write_client = FakeLLMClient(text="should never be called")

        new_entities, pending_updates, updated_uids, escalations = (
            tier3_derive_actions(raw, actions, index, wiki_root, write_client)
        )

        assert new_entities == []
        assert pending_updates == []
        assert updated_uids == []
        assert escalations == []
        # No LLM call was made either -- the missing-target check is BEFORE
        # any client.messages.create call in the update branch.
        assert write_client.calls == []
        # And, mechanically, no file landed anywhere under wiki_root.
        assert list(wiki_root.glob("*.md")) == []

    def test_merge_response_cannot_set_type_even_via_the_write_boundary(
        self, tmp_path: Path
    ) -> None:
        """Belt-and-suspenders: even if a future code path DID let a merge
        response influence frontmatter beyond `updated`/`field_sources`, the
        athenaeum#1196 write-boundary guard (_apply_tier3_results, which every
        Tier-3 write -- create AND the merge-adjacent pending_updates list --
        passes through) still only type-guards NEW entity writes. Documented
        here as an explicit non-goal: an existing page's type is never
        re-validated on every merge (out of athenaeum#1196's scope guard --
        "do not widen this into a schema refactor"); the guarantee for an
        EXISTING page's type not drifting rests on merge structurally never
        writing it (proven above), not on this guard re-checking it on
        every update.
        """
        wiki_root = _wiki_root(tmp_path)
        (wiki_root / "a1b2c3d4-acme-corp.md").write_text(
            "---\nuid: a1b2c3d4\ntype: company\nname: Acme Corp\n---\n\nbody\n",
            encoding="utf-8",
        )
        index = EntityIndex(wiki_root)
        raw = _raw("Acme Corp note.")
        result = ProcessingResult(raw_file=raw)

        # pending_updates is exactly the shape tier3_derive_actions returns
        # for a successful merge: (path, full_rendered_text). The type byte
        # sequence inside it is whatever the EXISTING page's meta carried --
        # this call cannot introduce a new one no matter what.
        pending_updates = [
            (
                wiki_root / "a1b2c3d4-acme-corp.md",
                "---\nuid: a1b2c3d4\ntype: company\nname: Acme Corp\n---\n\nnew body\n",
            )
        ]
        _apply_tier3_results(
            result,
            new_entities=[],
            pending_updates=pending_updates,
            updated_uids=["a1b2c3d4"],
            escalations=[],
            wiki_root=wiki_root,
            index=index,
            config=None,
        )
        meta, _body = parse_frontmatter(
            (wiki_root / "a1b2c3d4-acme-corp.md").read_text(encoding="utf-8")
        )
        assert meta["type"] == "company"


# ---------------------------------------------------------------------------
# AC3 wiring — the boundary guard fires even on a path that bypassed every
# upstream clamp (simulated here by constructing a WikiEntity directly,
# standing in for "a path we did not trace").
# ---------------------------------------------------------------------------


class TestAC3BoundaryGuardWiredIntoTheRealWriteSites:
    def test_apply_tier3_results_refuses_a_foreign_type_new_entity(
        self, tmp_path: Path
    ) -> None:
        wiki_root = _wiki_root(tmp_path)
        wiki_root.mkdir(exist_ok=True)
        index = EntityIndex(wiki_root)
        raw = _raw("irrelevant")
        result = ProcessingResult(raw_file=raw)

        # A WikiEntity with a type no upstream clamp would ever admit --
        # simulating a path this issue did not trace (any future tier-3
        # variant, a hand-rolled importer, etc.) reaching this write
        # boundary with an unclamped type.
        rogue = WikiEntity(
            uid="dead1234",
            type="issue",
            name="Some GitHub issue",
            body="body",
        )

        _apply_tier3_results(
            result,
            new_entities=[rogue],
            pending_updates=[],
            updated_uids=[],
            escalations=[],
            wiki_root=wiki_root,
            index=index,
            config=None,
        )

        assert result.created == []
        assert result.type_rejected == 1
        assert not (wiki_root / rogue.filename).exists()
        assert (wiki_root / TYPE_REJECTED_DIR_NAME / rogue.filename).exists()
        records = list_type_rejected(wiki_root)
        assert len(records) == 1
        assert records[0]["type"] == "issue"
        assert records[0]["source"] == "tier3-create"

    def test_declared_type_new_entity_still_writes_normally(
        self, tmp_path: Path
    ) -> None:
        """Non-regression: a legitimate declared-type write still passes
        through the guard untouched."""
        wiki_root = _wiki_root(tmp_path)
        wiki_root.mkdir(exist_ok=True)
        index = EntityIndex(wiki_root)
        raw = _raw("irrelevant")
        result = ProcessingResult(raw_file=raw)

        good = WikiEntity(
            uid="abc12345",
            type="reference",
            name="A legitimate reference page",
            body="body",
        )

        _apply_tier3_results(
            result,
            new_entities=[good],
            pending_updates=[],
            updated_uids=[],
            escalations=[],
            wiki_root=wiki_root,
            index=index,
            config=None,
        )

        assert len(result.created) == 1
        assert result.type_rejected == 0
        assert (wiki_root / good.filename).exists()

    def test_declared_entity_classes_agree_with_the_fixture(
        self, tmp_path: Path
    ) -> None:
        """Sanity check on this file's own fixture, not the guard itself:
        confirms _DECLARED_TYPES round-trips through the real types.md
        reader, so the assertions above are checking the real declared set,
        not an assumed one."""
        wiki_root = _wiki_root(tmp_path)
        declared = declared_entity_classes(wiki_root)
        assert declared == frozenset(_DECLARED_TYPES)
        assert "auto-memory" not in declared
        assert "auto-memory" in KNOWN_TYPES
