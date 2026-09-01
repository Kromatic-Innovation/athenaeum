# SPDX-License-Identifier: Apache-2.0
"""Tests for the consult-only person registry (issue athenaeum#1183).

Covers all four acceptance criteria:

1. ``type: person`` pages are withheld from :class:`~athenaeum.models.EntityIndex`'s
   raw-text MATCHING surface (:func:`~athenaeum.tiers.tier1_programmatic_match`),
   while every name/uid-ADDRESSED lookup keeps finding one exactly as before
   (backward compatible with an unmigrated corpus).
2. :func:`~athenaeum.identity_resolution.resolve_person_mention` +
   :func:`~athenaeum.intake.attribute_person_observation` resolve and
   attribute a person mention via the registry when the entity index has no
   entry for it.
3. :func:`~athenaeum.intake.tier0_passthrough` /
   :func:`~athenaeum.librarian.tier0_handle_upsert` apply structured field
   updates to a registry person record with ZERO LLM provider calls.
4. :func:`~athenaeum.tiers.tier3_merge` / ``tier3_merge_full`` / ``tier3_write``
   / ``tier3_create`` refuse a ``type: person`` target before any provider
   call.

``TestProductionRoundTrip`` drives an ordinary free-text raw file mentioning
an EXISTING ``type: person`` page through the real ``athenaeum.librarian.run()``
dispatch cascade (not just ``process_one`` directly) and proves the mention
resolves, the observation is captured durably, zero provider calls are made,
and the file never lands on the stuck-file ledger.

All fixtures are synthetic — no client data lives in this public repo.
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from athenaeum.identity_resolution import resolve_person_mention
from athenaeum.intake import attribute_person_observation, tier0_passthrough
from athenaeum.librarian import process_one, tier0_handle_upsert
from athenaeum.models import EntityAction, EntityIndex, RawFile
from athenaeum.person_registry import (
    PersonRegistry,
    PersonRegistryEntry,
    apply_person_field_update,
)
from athenaeum.tiers import (
    PersonNeverLLMRewriteError,
    tier1_programmatic_match,
    tier3_create,
    tier3_merge,
    tier3_merge_full,
    tier3_write,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _write_person(
    root: Path,
    *,
    uid: str,
    name: str,
    extra_fm: str = "",
    body: str = "Body.\n",
) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    filename = f"{uid}-{name.lower().replace(' ', '-')}.md"
    path = root / filename
    path.write_text(
        f"---\nuid: {uid}\ntype: person\nname: {name}\n{extra_fm}---\n\n# {name}\n\n{body}",
        encoding="utf-8",
    )
    return path


def _write_company(root: Path, *, uid: str, name: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    filename = f"{uid}-{name.lower().replace(' ', '-')}.md"
    path = root / filename
    path.write_text(
        f"---\nuid: {uid}\ntype: company\nname: {name}\n---\n\n# {name}\n\nBody.\n",
        encoding="utf-8",
    )
    return path


def _make_raw(content: str, path: Path | None = None) -> RawFile:
    return RawFile(
        path=path or Path("/tmp/fake/sessions/20240407T120000Z-aabb0011.md"),
        source="sessions",
        timestamp="20240407T120000Z",
        uuid8="aabb0011",
        _content=content,
    )


class _FakeClient:
    """Records every ``messages.create`` call for a zero-calls assertion."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.messages = self

    def create(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        response = MagicMock()
        response.content = [MagicMock(text="# Should Not Reach Here\n\nbody.")]
        response.stop_reason = "end_turn"
        return response


# ---------------------------------------------------------------------------
# AC1 — demotion out of the entity-index MATCHING surface
# ---------------------------------------------------------------------------


class TestAC1EntityIndexDemotion:
    def test_tier1_never_matches_a_person_name(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        _write_person(wiki, uid="person1a", name="Alice Zhang")
        index = EntityIndex(wiki)

        raw = _make_raw("Had coffee with Alice Zhang yesterday.")
        matched = tier1_programmatic_match(raw, index)
        assert "alice zhang" not in {n for n, _, _ in matched}

    def test_items_withholds_person_but_a_sibling_type_still_matches(
        self, tmp_path: Path
    ) -> None:
        wiki = tmp_path / "wiki"
        _write_person(wiki, uid="person1a", name="Alice Zhang")
        _write_company(wiki, uid="company1", name="Widget Traders")
        index = EntityIndex(wiki)

        keys = dict(index.items())
        assert "alice zhang" not in keys
        assert "widget traders" in keys

    def test_lookup_still_finds_a_person_by_name(self, tmp_path: Path) -> None:
        """Backward compat: a structured, name-ADDRESSED lookup (as opposed
        to raw-text MATCHING) is unaffected — this is what
        athenaeum.corrections.resolve_target and tier3's create-name
        collision check rely on."""
        wiki = tmp_path / "wiki"
        _write_person(wiki, uid="person1a", name="Alice Zhang")
        index = EntityIndex(wiki)

        hit = index.lookup("Alice Zhang")
        assert hit is not None
        assert hit.type == "person"

    def test_get_by_uid_still_finds_a_person_page(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        page = _write_person(wiki, uid="person1a", name="Alice Zhang")
        index = EntityIndex(wiki)
        assert index.get_by_uid("person1a") == page


# ---------------------------------------------------------------------------
# AC2 — intake consult + attribution
# ---------------------------------------------------------------------------


class TestAC2RegistryConsult:
    def test_resolve_person_mention_finds_a_relocated_person_via_registry(
        self, tmp_path: Path
    ) -> None:
        """Simulates the post-athenaeum#1247 shape: the person page lives
        OUTSIDE wiki_root (entity_index has no entry for it at all), while
        the person registry — pointed at the relocated root — still
        resolves the mention."""
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        registry_root = tmp_path / "registry"
        _write_person(registry_root, uid="person1a", name="Alice Zhang")

        index = EntityIndex(wiki)  # empty — person page not here
        registry = PersonRegistry(registry_root)

        entry = resolve_person_mention(
            "Alice Zhang", wiki_root=wiki, entity_index=index, registry=registry
        )
        assert entry is not None
        assert entry.uid == "person1a"

    def test_resolve_person_mention_engages_on_an_unmigrated_corpus(
        self, tmp_path: Path
    ) -> None:
        """On an UNMIGRATED corpus the person page still lives under
        wiki_root, so entity_index.lookup ALSO finds it (AC1's backward
        compat) -- that is the ORDINARY case, not a reason to defer. Only a
        hit belonging to a DIFFERENT (non-person) entity should defer; a
        person-typed hit must not shadow the registry consult, or it would
        never engage on today's corpus at all."""
        wiki = tmp_path / "wiki"
        _write_person(wiki, uid="person1a", name="Alice Zhang")
        index = EntityIndex(wiki)
        registry = PersonRegistry(wiki)

        entry = resolve_person_mention(
            "Alice Zhang", wiki_root=wiki, entity_index=index, registry=registry
        )
        assert entry is not None
        assert entry.uid == "person1a"

    def test_resolve_person_mention_defers_to_a_same_named_non_person_entity(
        self, tmp_path: Path
    ) -> None:
        """A genuine collision -- a differently-typed page sharing the same
        name -- is authoritative; the person registry must not silently
        shadow it."""
        wiki = tmp_path / "wiki"
        _write_company(wiki, uid="company1", name="Alice Zhang")
        registry_root = tmp_path / "registry"
        _write_person(registry_root, uid="person1a", name="Alice Zhang")
        index = EntityIndex(wiki)
        registry = PersonRegistry(registry_root)

        entry = resolve_person_mention(
            "Alice Zhang", wiki_root=wiki, entity_index=index, registry=registry
        )
        assert entry is None

    def test_resolve_person_mention_none_when_nobody_matches(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        index = EntityIndex(wiki)
        registry = PersonRegistry(tmp_path / "registry")
        entry = resolve_person_mention(
            "Nobody At All", wiki_root=wiki, entity_index=index, registry=registry
        )
        assert entry is None

    def test_attribute_person_observation_prepends_dated_bullet_under_notes(
        self, tmp_path: Path
    ) -> None:
        registry_root = tmp_path / "registry"
        page = _write_person(
            registry_root,
            uid="person1a",
            name="Alice Zhang",
            body="# Alice Zhang\n\n## Notes\n\n- 2026-01-01: old note\n",
        )
        entry = PersonRegistryEntry(uid="person1a", path=page, name="Alice Zhang")
        raw = _make_raw("Alice mentioned she's now leading the platform team.")

        changed = attribute_person_observation(raw, entry)
        assert changed is True
        text = page.read_text(encoding="utf-8")
        assert "leading the platform team" in text
        assert "old note" in text  # not clobbered

    def test_attribute_person_observation_creates_notes_section_if_absent(
        self, tmp_path: Path
    ) -> None:
        registry_root = tmp_path / "registry"
        page = _write_person(registry_root, uid="person1a", name="Alice Zhang")  # no ## Notes
        entry = PersonRegistryEntry(uid="person1a", path=page, name="Alice Zhang")
        raw = _make_raw("First observation about Alice.")

        assert attribute_person_observation(raw, entry) is True
        assert "## Notes" in page.read_text(encoding="utf-8")
        assert "First observation about Alice." in page.read_text(encoding="utf-8")

    def test_attribute_person_observation_noop_on_empty_body(self, tmp_path: Path) -> None:
        registry_root = tmp_path / "registry"
        page = _write_person(registry_root, uid="person1a", name="Alice Zhang")
        entry = PersonRegistryEntry(uid="person1a", path=page, name="Alice Zhang")
        raw = _make_raw("---\nsource: manual\n---\n\n   \n")
        assert attribute_person_observation(raw, entry) is False


# ---------------------------------------------------------------------------
# AC3 — tier-0 structured field updates, LLM-free
# ---------------------------------------------------------------------------


class TestAC3TierZeroNoLLM:
    def test_tier0_passthrough_creates_new_person_in_the_registry(
        self, tmp_path: Path
    ) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        registry_root = tmp_path / "registry"
        index = EntityIndex(wiki)
        registry = PersonRegistry(registry_root)

        raw = _make_raw(
            "---\nuid: person1a\ntype: person\nname: Alice Zhang\n---\n\n"
            "# Alice Zhang\n\nProduct lead.\n"
        )

        entity = tier0_passthrough(raw, index, wiki, ["person"], person_registry=registry)
        assert entity is not None
        assert entity.type == "person"

        # Landed under the registry root, NOT wiki_root.
        assert not (wiki / "person1a-alice-zhang.md").exists()
        assert (registry_root / "person1a-alice-zhang.md").exists()

        # Registered into the PERSON registry, never the general entity index.
        assert registry.get_by_uid("person1a") is not None
        assert index.get_by_uid("person1a") is None
        assert index.lookup("Alice Zhang") is None

    def test_tier0_passthrough_default_unwired_is_byte_identical(
        self, tmp_path: Path
    ) -> None:
        """person_registry=None (every pre-athenaeum#1183 caller) keeps a
        `type: person` raw on the ORIGINAL wiki_root/index path."""
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        index = EntityIndex(wiki)
        raw = _make_raw(
            "---\nuid: person1a\ntype: person\nname: Alice Zhang\n---\n\n"
            "# Alice Zhang\n\nProduct lead.\n"
        )
        entity = tier0_passthrough(raw, index, wiki, ["person"])
        assert entity is not None
        assert (wiki / "person1a-alice-zhang.md").exists()
        assert index.get_by_uid("person1a") is not None

    def test_tier0_handle_upsert_applies_field_update_to_registry_record(
        self, tmp_path: Path
    ) -> None:
        wiki = tmp_path / "wiki"
        registry_root = tmp_path / "registry"
        page = _write_person(
            registry_root, uid="person1a", name="Alice Zhang", extra_fm="linkedin_url: ''\n"
        )
        registry = PersonRegistry(registry_root)
        index = EntityIndex(wiki)

        raw = _make_raw(
            "---\nuid: person1a\ntype: person\nname: Alice Zhang\n"
            "linkedin_url: https://linkedin.com/in/alicezhang\n---\n\n# Alice Zhang\n\nseed\n"
        )

        out = tier0_handle_upsert(raw, index, wiki, ["person"], person_registry=registry)
        assert out is not None
        entity, changed = out
        assert changed is True
        assert entity.uid == "person1a"
        text = page.read_text(encoding="utf-8")
        assert "linkedin.com/in/alicezhang" in text
        assert "Body." in text  # body untouched, not flattened into prose

    def test_tier0_handle_upsert_uidless_resolves_via_registry_not_index(
        self, tmp_path: Path
    ) -> None:
        """The name/alias fallback (no uid declared in the seed) resolves
        against the registry when one is supplied — index alone (scoped to
        an empty wiki_root here) would find nothing."""
        wiki = tmp_path / "wiki"
        registry_root = tmp_path / "registry"
        page = _write_person(registry_root, uid="person1a", name="Alice Zhang")
        registry = PersonRegistry(registry_root)
        index = EntityIndex(wiki)  # empty — proves resolution came via registry

        raw = _make_raw(
            "---\ntype: person\nname: Alice Zhang\n"
            "linkedin_url: https://linkedin.com/in/alicezhang\n---\n\n# seed\n"
        )
        out = tier0_handle_upsert(raw, index, wiki, ["person"], person_registry=registry)
        assert out is not None
        entity, changed = out
        assert changed is True
        assert entity.uid == "person1a"
        assert "linkedin.com/in/alicezhang" in page.read_text(encoding="utf-8")

    def test_process_one_creates_new_person_via_registry_zero_provider_calls(
        self, tmp_path: Path
    ) -> None:
        """End-to-end through process_one (the real dispatch cascade): a
        brand-new `type: person` raw is created via the registry-routed
        tier0_passthrough branch, and the LLM client — reachable, but
        threaded only for tier2/tier3 — is NEVER called."""
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        registry_root = tmp_path / "registry"
        index = EntityIndex(wiki)
        registry = PersonRegistry(registry_root)

        raw_dir = tmp_path / "raw" / "contact-wiki"
        raw_dir.mkdir(parents=True)
        raw_path = raw_dir / "seed.md"
        raw_path.write_text(
            "---\nuid: person1a\ntype: person\nname: Alice Zhang\n---\n\n"
            "# Alice Zhang\n\nProduct lead.\n",
            encoding="utf-8",
        )
        raw = RawFile(path=raw_path, source="contact-wiki", timestamp="", uuid8="")

        client = _FakeClient()
        process_one(
            raw, index, wiki, client, ["person"], [], ["internal"], person_registry=registry
        )

        assert client.calls == []
        assert registry.get_by_uid("person1a") is not None
        assert index.get_by_uid("person1a") is None

    def test_process_one_updates_existing_person_via_registry_zero_provider_calls(
        self, tmp_path: Path
    ) -> None:
        """Same end-to-end proof for the UPDATE (tier0_handle_upsert)
        branch: a structured field-update seed for an EXISTING person
        record never reaches the client either."""
        wiki = tmp_path / "wiki"
        registry_root = tmp_path / "registry"
        page = _write_person(
            registry_root, uid="person1a", name="Alice Zhang", extra_fm="linkedin_url: ''\n"
        )
        registry = PersonRegistry(registry_root)
        index = EntityIndex(wiki)

        raw_dir = tmp_path / "raw" / "contact-wiki"
        raw_dir.mkdir(parents=True)
        raw_path = raw_dir / "seed.md"
        raw_path.write_text(
            "---\nuid: person1a\ntype: person\nname: Alice Zhang\n"
            "linkedin_url: https://linkedin.com/in/alicezhang\n---\n\n# Alice Zhang\n\nseed\n",
            encoding="utf-8",
        )
        raw = RawFile(path=raw_path, source="contact-wiki", timestamp="", uuid8="")

        client = _FakeClient()
        process_one(
            raw, index, wiki, client, ["person"], [], ["internal"], person_registry=registry
        )

        assert client.calls == []
        assert "linkedin.com/in/alicezhang" in page.read_text(encoding="utf-8")

    def test_apply_person_field_update_idempotent_no_delta(self, tmp_path: Path) -> None:
        registry_root = tmp_path / "registry"
        page = _write_person(
            registry_root, uid="person1a", name="Alice Zhang", extra_fm="current_title: Lead\n"
        )
        before = page.read_text(encoding="utf-8")
        meta, changed = apply_person_field_update(page, {"current_title": "Lead"})
        assert changed is False
        assert page.read_text(encoding="utf-8") == before  # byte-for-byte stable

    def test_apply_person_field_update_writes_a_real_delta(self, tmp_path: Path) -> None:
        registry_root = tmp_path / "registry"
        page = _write_person(
            registry_root, uid="person1a", name="Alice Zhang", extra_fm="current_title: Lead\n"
        )
        meta, changed = apply_person_field_update(page, {"current_title": "Director"})
        assert changed is True
        assert meta["current_title"] == "Director"
        assert "current_title: Director" in page.read_text(encoding="utf-8")

    def test_apply_person_field_update_dry_run_does_not_write(self, tmp_path: Path) -> None:
        registry_root = tmp_path / "registry"
        page = _write_person(
            registry_root, uid="person1a", name="Alice Zhang", extra_fm="current_title: Lead\n"
        )
        before = page.read_text(encoding="utf-8")
        meta, changed = apply_person_field_update(
            page, {"current_title": "Director"}, dry_run=True
        )
        assert changed is True
        assert meta["current_title"] == "Director"
        assert page.read_text(encoding="utf-8") == before  # nothing written


# ---------------------------------------------------------------------------
# AC4 — never a tier-3 full-page LLM rewrite
# ---------------------------------------------------------------------------


def _person_action(existing_uid: str | None = "person1a") -> EntityAction:
    return EntityAction(
        kind="update" if existing_uid else "create",
        name="Alice Zhang",
        entity_type="person",
        tags=[],
        access="internal",
        existing_uid=existing_uid,
        observations="Some observation about Alice.",
    )


class TestAC4NeverTier3Rewrite:
    def test_tier3_merge_refuses_before_any_provider_call(self) -> None:
        client = _FakeClient()
        with pytest.raises(PersonNeverLLMRewriteError):
            tier3_merge(_person_action(), "Existing body.", "sessions/raw.md", client)
        assert client.calls == []

    def test_tier3_merge_full_refuses_before_any_provider_call(self) -> None:
        client = _FakeClient()
        with pytest.raises(PersonNeverLLMRewriteError):
            tier3_merge_full(_person_action(), "Existing body.", "sessions/raw.md", client)
        assert client.calls == []

    def test_tier3_create_refuses_before_any_provider_call(self) -> None:
        client = _FakeClient()
        action = _person_action(existing_uid=None)
        with pytest.raises(PersonNeverLLMRewriteError):
            tier3_create(action, "sessions/raw.md", client)
        assert client.calls == []

    def test_tier3_write_refuses_the_whole_batch_before_any_provider_call(
        self, tmp_path: Path
    ) -> None:
        """A person action mixed alongside an ordinary company action must
        refuse the WHOLE batch up front — the company action's call must
        never fire either, proving the guard runs before any dispatch."""
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        index = EntityIndex(wiki)
        client = _FakeClient()
        raw = _make_raw("Something about Alice and Acme.")
        actions = [
            _person_action(existing_uid=None),
            EntityAction(
                kind="create",
                name="Acme Corp",
                entity_type="company",
                tags=[],
                access="internal",
                existing_uid=None,
                observations="text",
            ),
        ]
        with pytest.raises(PersonNeverLLMRewriteError):
            tier3_write(raw, actions, index, wiki, client)
        assert client.calls == []


# ---------------------------------------------------------------------------
# Production round-trip — the real dispatch cascade, not process_one directly
# ---------------------------------------------------------------------------


class TestProductionRoundTrip:
    """An ordinary free-text raw file mentioning an EXISTING `type: person`
    page, driven through the real `athenaeum.librarian.run()` pipeline
    (issue athenaeum#1183 AC2/AC3, required before merge per Occam).

    Before `resolve_person_mention` / `attribute_person_observation` were
    wired into `process_one`'s dispatch cascade, this exact scenario was
    broken: `EntityIndex.items()` withholds `type: person` (AC1), so
    `tier1_programmatic_match` never matches the mention; tier2 then
    classifies it as a NEW entity (no `existing_uid`); tier3_create raises
    `PersonNeverLLMRewriteError`; the run's generic per-file exception
    handler catches it, logs it, and moves on -- the observation is lost
    forever and the file is stuck on this corpus permanently. This class
    proves that no longer happens.
    """

    def _seed_knowledge_root(self, tmp_path: Path) -> Path:
        root = tmp_path / "knowledge"
        root.mkdir()

        wiki = root / "wiki"
        (wiki / "_schema").mkdir(parents=True)
        (wiki / "_schema" / "types.md").write_text(
            "# Types\n\n| Type |\n|------|\n| person |\n| company |\n"
        )
        (wiki / "_schema" / "tags.md").write_text(
            "# Tags\n\n| Tag |\n|-----|\n| active |\n"
        )
        (wiki / "_schema" / "access-levels.md").write_text(
            "# Access\n\n| Level |\n|-------|\n| internal |\n"
        )

        # An EXISTING person page -- the on-disk shape an UNMIGRATED corpus
        # has today: still physically under wiki/, same as every other
        # entity page, exactly what athenaeum#1183 does NOT change.
        _write_person(
            wiki,
            uid="person1a",
            name="Alice Zhang",
            body="# Alice Zhang\n\n## Notes\n\n- 2026-01-01: Joined as product lead.\n",
        )

        sessions = root / "raw" / "sessions"
        sessions.mkdir(parents=True)
        (sessions / ".gitkeep").write_text("")

        subprocess.run(["git", "init", "-q", "-b", "test-branch"], cwd=root, check=True)
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"], cwd=root, check=True
        )
        subprocess.run(["git", "config", "user.name", "Test Runner"], cwd=root, check=True)
        subprocess.run(["git", "add", "-A"], cwd=root, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=root, check=True)

        # Dropped post-commit so it is an uncommitted change when run() takes
        # its pre-processing snapshot -- ordinary, unstructured free-text
        # prose (no frontmatter at all), mentioning the EXISTING person by
        # name. This is the shape that broke before this fix.
        (sessions / "20240410T120000Z-aabbccdd.md").write_text(
            "Caught up with Alice Zhang today -- she's now running the "
            "platform team and shipped the new onboarding flow.\n"
        )
        return root

    def test_ordinary_mention_of_existing_person_round_trips_with_no_llm(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        import anthropic as anthropic_mod

        from athenaeum.librarian import run

        root = self._seed_knowledge_root(tmp_path)
        person_page = root / "wiki" / "person1a-alice-zhang.md"
        before = person_page.read_text(encoding="utf-8")

        # A client that RECORDS any call made to it (via Mock's own call
        # tracking, asserted below) rather than raising -- if the mention is
        # not intercepted by the tier-0 registry consult, tier2_classify is
        # the very next thing that would call this, and letting it return a
        # harmless canned response keeps the rest of the run's error
        # handling from masking the real signal.
        classify_response = MagicMock()
        classify_response.content = [MagicMock(text=json.dumps([]))]
        mock_client = MagicMock()
        mock_client.messages.create.return_value = classify_response
        monkeypatch.setattr(anthropic_mod, "Anthropic", lambda **kwargs: mock_client)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-api-key-not-real")
        caplog.set_level(logging.INFO, logger="athenaeum")

        exit_code = run(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            max_api_calls=10,
        )

        # (b) zero provider calls.
        mock_client.messages.create.assert_not_called()

        # (a) the observation is captured/attributed somewhere durable: the
        # EXISTING person page's body, on disk.
        after = person_page.read_text(encoding="utf-8")
        assert after != before
        assert "platform team" in after
        assert "Joined as product lead" in after  # prior note not clobbered

        # (c) PersonNeverLLMRewriteError did not fire, and the file did not
        # land on the failed-files / stuck-file ledger.
        assert exit_code == 0
        joined_log = "\n".join(rec.message for rec in caplog.records)
        assert "PersonNeverLLMRewriteError" not in joined_log
        assert "entity-file-failure" not in joined_log
        assert "Failed files" not in joined_log
        stuck_ledger = root / "wiki" / "_stuck_files.json"
        if stuck_ledger.exists():
            assert "20240410T120000Z-aabbccdd" not in stuck_ledger.read_text(
                encoding="utf-8"
            )

        # The raw file was consumed (retire-on-success semantics), not left
        # to be reprocessed identically forever.
        assert not (root / "raw" / "sessions" / "20240410T120000Z-aabbccdd.md").exists()
