# SPDX-License-Identifier: Apache-2.0
"""Owner as a standing person-hint candidate (athenaeum#2653).

Drives the shared candidate helper (:mod:`athenaeum.person_hints`), the
classify/verify prompt rendering, and both intake transports
(:func:`athenaeum.librarian.process_one` and
:func:`athenaeum.batch.process_batch_run`) with a queued fake LLM backend --
no live API. All fixtures are synthetic.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from athenaeum.batch import process_batch_run
from athenaeum.intake import PERSON_OBSERVATION_MAX_FANOUT
from athenaeum.librarian import process_one
from athenaeum.mcp_server import remember_write
from athenaeum.models import ClassifiedEntity, EntityAction, EntityIndex, RawFile, TokenUsage
from athenaeum.person_hints import (
    PERSON_HINT_MAX_CANDIDATES,
    build_person_hints,
    cap_hint_claims,
)
from athenaeum.person_registry import PersonRegistry
from athenaeum.tiers import (
    PERSON_HINT_CLASSIFY_PROMPT,
    PERSON_HINT_OWNER_CLASSIFY_PROMPT,
    PERSON_HINT_OWNER_VERIFY_NOTE,
    PERSON_HINT_VERIFY_NOTE,
    _render_person_candidates_section,
    tier3_merge_params,
)

OWNER_UID = "own00001"
VALID_TYPES = ["person", "company"]
VALID_TAGS = ["active"]
VALID_ACCESS = ["internal"]
CONFIG = {"owner": {"uid": OWNER_UID, "aliases": ["Riley Quill"]}}


def _write_person(wiki: Path, uid: str, name: str, body: str = "Body.\n") -> Path:
    wiki.mkdir(parents=True, exist_ok=True)
    path = wiki / f"{uid}-{name.lower().replace(' ', '-')}.md"
    path.write_text(
        f"---\nuid: {uid}\ntype: person\nname: {name}\n---\n\n# {name}\n\n{body}",
        encoding="utf-8",
    )
    return path


def _raw(path: Path, content: str) -> RawFile:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return RawFile(path=path, source=path.parent.name, timestamp="", uuid8="")


class _Queued:
    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []
        self.messages = self

    def create(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        if not self._responses:
            raise AssertionError(f"no more queued responses (call #{len(self.calls)})")
        response = MagicMock()
        response.content = [MagicMock(text=self._responses.pop(0))]
        response.stop_reason = "end_turn"
        return response


def _corpus(tmp_path: Path) -> tuple[Path, Path, EntityIndex, PersonRegistry]:
    knowledge = tmp_path / "knowledge"
    wiki = knowledge / "wiki"
    owner_page = _write_person(wiki, OWNER_UID, "Riley Quill", "The owner.\n")
    return wiki, owner_page, EntityIndex(wiki), PersonRegistry(wiki)


def _ops(text: str) -> str:
    return json.dumps({"ops": [{"op": "append_section", "text": text}], "adds_new_claim": True})


class TestCandidateHelper:
    def test_owner_offered_with_no_literal_match(self, tmp_path: Path) -> None:
        wiki, _, index, registry = _corpus(tmp_path)
        raw = _raw(tmp_path / "raw" / "s" / "a.md", "I prefer short emails.")
        hs = build_person_hints(raw, wiki, index, registry, CONFIG)
        assert [c["uid"] for c in hs.candidates] == [OWNER_UID]
        assert hs.candidates[0]["role"] == "owner"
        assert hs.owner_uid == OWNER_UID

    def test_owner_in_reserved_slot_outside_candidate_cap(self, tmp_path: Path) -> None:
        wiki, _, index, registry = _corpus(tmp_path)
        names = [f"Person Number{chr(97 + i)}x" for i in range(PERSON_HINT_MAX_CANDIDATES + 3)]
        for i, n in enumerate(names):
            _write_person(wiki, f"p{i:07d}", n)
        index, registry = EntityIndex(wiki), PersonRegistry(wiki)
        raw = _raw(tmp_path / "raw" / "s" / "a.md", "Met " + ", ".join(names) + ".")
        hs = build_person_hints(raw, wiki, index, registry, CONFIG)
        assert len(hs.candidates) == PERSON_HINT_MAX_CANDIDATES + 1
        assert hs.candidates[-1]["uid"] == OWNER_UID
        # Without an owner the cap alone applies.
        plain = build_person_hints(raw, wiki, index, registry, None)
        assert len(plain.candidates) == PERSON_HINT_MAX_CANDIDATES
        assert plain.owner_uid is None

    def test_owner_deduplicated_when_matched_literally(self, tmp_path: Path) -> None:
        wiki, _, index, registry = _corpus(tmp_path)
        raw = _raw(tmp_path / "raw" / "s" / "a.md", "Riley Quill decided to ship Friday.")
        hs = build_person_hints(raw, wiki, index, registry, CONFIG)
        assert [c["uid"] for c in hs.candidates] == [OWNER_UID]
        assert hs.candidates[0]["role"] == "owner"

    def test_no_owner_is_unchanged(self, tmp_path: Path) -> None:
        wiki, _, index, registry = _corpus(tmp_path)
        raw = _raw(tmp_path / "raw" / "s" / "a.md", "I prefer short emails.")
        for cfg in (None, {}, {"owner": {"aliases": ["Riley Quill"]}}):
            hs = build_person_hints(raw, wiki, index, registry, cfg)
            assert hs.candidates == [] and hs.owner_uid is None

    def test_owner_without_a_page_is_not_offered(self, tmp_path: Path) -> None:
        wiki, owner_page, _, _ = _corpus(tmp_path)
        owner_page.unlink()
        raw = _raw(tmp_path / "raw" / "s" / "a.md", "I prefer short emails.")
        hs = build_person_hints(raw, wiki, EntityIndex(wiki), PersonRegistry(wiki), CONFIG)
        assert hs.candidates == []

    def test_works_without_a_registry(self, tmp_path: Path) -> None:
        wiki, _, index, _ = _corpus(tmp_path)
        raw = _raw(tmp_path / "raw" / "s" / "a.md", "I prefer short emails.")
        hs = build_person_hints(raw, wiki, index, None, CONFIG)
        assert [c["uid"] for c in hs.candidates] == [OWNER_UID]

    def test_structured_jsonl_machine_records_get_no_owner(self, tmp_path: Path) -> None:
        wiki, _, index, registry = _corpus(tmp_path)
        raw = _raw(tmp_path / "raw" / "s" / "a.jsonl", '{"email": "x@example.com"}\n')
        assert build_person_hints(raw, wiki, index, registry, CONFIG).candidates == []

    def test_owner_claim_exempt_from_fanout_cap(self) -> None:
        def claim(uid: str) -> ClassifiedEntity:
            return ClassifiedEntity(
                name=uid,
                entity_type="person",
                tags=[],
                access="",
                is_new=False,
                existing_uid=uid,
                observations="x",
                from_person_hint=True,
            )

        others = [claim(f"u{i}") for i in range(PERSON_OBSERVATION_MAX_FANOUT + 2)]
        kept, dropped = cap_hint_claims([claim(OWNER_UID), *others], OWNER_UID, "r")
        assert len(kept) == PERSON_OBSERVATION_MAX_FANOUT + 1
        assert OWNER_UID in [c.existing_uid for c in kept]
        assert dropped == ["u5", "u6"]
        # Owner claim arriving after the cap is reached is still kept.
        kept2, _ = cap_hint_claims([*others, claim(OWNER_UID)], OWNER_UID, "r")
        assert kept2[-1].existing_uid == OWNER_UID


class TestPromptText:
    def test_owner_text_lives_in_its_own_prompt_files(self) -> None:
        assert "(owner)" not in PERSON_HINT_CLASSIFY_PROMPT
        assert "literal name/alias search" in PERSON_HINT_CLASSIFY_PROMPT
        assert "first person" in PERSON_HINT_OWNER_CLASSIFY_PROMPT
        assert "## Preferences" in PERSON_HINT_OWNER_VERIFY_NOTE

    def test_no_owner_render_is_byte_identical(self) -> None:
        cands = [{"uid": "a1", "name": "Alice", "description": "d"}]
        out = _render_person_candidates_section(cands)
        assert PERSON_HINT_CLASSIFY_PROMPT in out
        assert PERSON_HINT_OWNER_CLASSIFY_PROMPT not in out
        assert "(owner)" not in out

    def test_owner_only_render_skips_literal_match_framing(self) -> None:
        cands = [{"uid": OWNER_UID, "name": "Riley Quill", "role": "owner"}]
        out = _render_person_candidates_section(cands)
        assert PERSON_HINT_OWNER_CLASSIFY_PROMPT in out
        assert PERSON_HINT_CLASSIFY_PROMPT not in out
        assert f"- {OWNER_UID}: Riley Quill (owner)" in out

    def test_mixed_render_carries_both_blocks(self) -> None:
        cands = [
            {"uid": "a1", "name": "Alice"},
            {"uid": OWNER_UID, "name": "Riley Quill", "role": "owner"},
        ]
        out = _render_person_candidates_section(cands)
        assert PERSON_HINT_OWNER_CLASSIFY_PROMPT in out and PERSON_HINT_CLASSIFY_PROMPT in out

    def test_verify_note_choice_follows_owner_uid(self) -> None:
        def note(uid: str, cfg: dict | None) -> str:
            action = EntityAction(
                kind="update",
                name="N",
                entity_type="",
                tags=[],
                access="",
                existing_uid=uid,
                observations="o",
                from_person_hint=True,
            )
            params = tier3_merge_params(action, "# N\n\nBody.\n", "ref", config=cfg)
            return params["messages"][0]["content"]

        assert PERSON_HINT_OWNER_VERIFY_NOTE in note(OWNER_UID, CONFIG)
        assert PERSON_HINT_VERIFY_NOTE not in note(OWNER_UID, CONFIG)
        assert PERSON_HINT_VERIFY_NOTE in note("other001", CONFIG)
        assert PERSON_HINT_VERIFY_NOTE in note(OWNER_UID, None)


def _run_sync(wiki: Path, raw: RawFile, client: _Queued, config: dict | None, registry):
    return process_one(
        raw,
        EntityIndex(wiki),
        wiki,
        client,
        VALID_TYPES,
        VALID_TAGS,
        VALID_ACCESS,
        usage=TokenUsage(),
        config=config,
        person_registry=registry,
    )


def _run_batch(wiki: Path, raw: RawFile, client: _Queued, config: dict | None, registry):
    return process_batch_run(
        [raw],
        EntityIndex(wiki),
        wiki,
        client,
        VALID_TYPES,
        VALID_TAGS,
        VALID_ACCESS,
        usage=TokenUsage(),
        config=config,
        max_api_calls=100,
        write_client=client,
        batch_classify=False,
        batch_write=False,
        person_registry=registry,
    )


@pytest.mark.parametrize("transport", ["sync", "batch"])
class TestRoutingAndParity:
    def _go(self, transport, wiki, raw, client, config, registry):
        fn = _run_sync if transport == "sync" else _run_batch
        return fn(wiki, raw, client, config, registry)

    def test_first_person_claim_updates_owner_page(self, transport, tmp_path: Path) -> None:
        wiki, owner_page, _, registry = _corpus(tmp_path)
        raw = _raw(
            tmp_path / "knowledge" / "raw" / "s" / "a.md", "I decided to move standup to 9am."
        )
        client = _Queued(
            [
                json.dumps([{"candidate_uid": OWNER_UID, "observations": "Moved standup to 9am."}]),
                _ops("- Moved standup to 9am."),
            ]
        )
        self._go(transport, wiki, raw, client, CONFIG, registry)
        assert "- Moved standup to 9am." in owner_page.read_text(encoding="utf-8")
        classify_prompt = client.calls[0]["messages"][0]["content"]
        assert f"{OWNER_UID}: Riley Quill (owner)" in classify_prompt
        merge_prompt = client.calls[1]["messages"][0]["content"]
        assert PERSON_HINT_OWNER_VERIFY_NOTE.strip().splitlines()[0] in merge_prompt

    def test_preference_lands_in_preferences_section(self, transport, tmp_path: Path) -> None:
        wiki, owner_page, _, registry = _corpus(tmp_path)
        raw = _raw(
            tmp_path / "knowledge" / "raw" / "s" / "a.md", "I prefer concise status updates."
        )
        client = _Queued(
            [
                json.dumps(
                    [
                        {
                            "candidate_uid": OWNER_UID,
                            "observations": "Preference: concise status updates.",
                        }
                    ]
                ),
                _ops("## Preferences\n\n- Concise status updates."),
            ]
        )
        self._go(transport, wiki, raw, client, CONFIG, registry)
        body = owner_page.read_text(encoding="utf-8")
        assert "## Preferences\n\n- Concise status updates." in body
        merge_prompt = client.calls[1]["messages"][0]["content"]
        assert "Preference:" in merge_prompt and "## Preferences" in merge_prompt

    def test_invented_uid_is_dropped(self, transport, tmp_path: Path) -> None:
        wiki, owner_page, _, registry = _corpus(tmp_path)
        before = owner_page.read_text(encoding="utf-8")
        raw = _raw(tmp_path / "knowledge" / "raw" / "s" / "a.md", "I like tea.")
        client = _Queued(
            [json.dumps([{"candidate_uid": "ghost999", "observations": "Likes tea."}])]
        )
        self._go(transport, wiki, raw, client, CONFIG, registry)
        assert owner_page.read_text(encoding="utf-8") == before
        assert len(client.calls) == 1  # no write call for an invented uid

    def test_tier3_verify_subject_mismatch_leaves_owner_page(
        self, transport, tmp_path: Path
    ) -> None:
        wiki, owner_page, _, registry = _corpus(tmp_path)
        before = owner_page.read_text(encoding="utf-8")
        raw = _raw(
            tmp_path / "knowledge" / "raw" / "s" / "a.md", "My manager said she prefers email."
        )
        client = _Queued(
            [
                json.dumps([{"candidate_uid": OWNER_UID, "observations": "Prefers email."}]),
                json.dumps({"ops": [], "adds_new_claim": False, "subject_mismatch": True}),
            ]
        )
        self._go(transport, wiki, raw, client, CONFIG, registry)
        assert owner_page.read_text(encoding="utf-8") == before
        assert len(client.calls) == 2

    def test_no_owner_configured_owner_claim_is_dropped_and_prompt_unchanged(
        self, transport, tmp_path: Path
    ) -> None:
        wiki, owner_page, _, registry = _corpus(tmp_path)
        before = owner_page.read_text(encoding="utf-8")
        raw = _raw(tmp_path / "knowledge" / "raw" / "s" / "a.md", "I prefer tea.")
        client = _Queued(
            [json.dumps([{"candidate_uid": OWNER_UID, "observations": "Prefers tea."}])]
        )
        self._go(transport, wiki, raw, client, None, registry)
        assert owner_page.read_text(encoding="utf-8") == before
        prompt = client.calls[0]["messages"][0]["content"]
        assert "Person hints" not in prompt and "(owner)" not in prompt


class TestParityAcrossTransports:
    def test_same_input_same_candidates_and_same_page(self, tmp_path: Path) -> None:
        outcomes = []
        for transport, fn in (("sync", _run_sync), ("batch", _run_batch)):
            root = tmp_path / transport
            wiki, owner_page, _, registry = _corpus(root)
            _write_person(wiki, "alice001", "Alice Zhang")
            registry = PersonRegistry(wiki)
            raw = _raw(
                root / "knowledge" / "raw" / "s" / "a.md",
                "Alice Zhang agreed. I prefer concise status updates.",
            )
            client = _Queued(
                [
                    json.dumps(
                        [
                            {
                                "candidate_uid": OWNER_UID,
                                "observations": "Preference: concise updates.",
                            },
                            {"candidate_uid": "alice001", "observations": "Agreed to the plan."},
                        ]
                    ),
                    _ops("- Agreed to the plan."),
                    _ops("## Preferences\n\n- Concise updates."),
                ]
            )
            fn(wiki, raw, client, CONFIG, registry)
            classify_prompt = client.calls[0]["messages"][0]["content"]
            candidates = classify_prompt.split("Candidates:")[1]
            outcomes.append(
                (
                    candidates,
                    len(client.calls),
                    owner_page.read_text(encoding="utf-8"),
                    (wiki / "alice001-alice-zhang.md").read_text(encoding="utf-8"),
                )
            )
        assert outcomes[0] == outcomes[1]
        assert "alice001: Alice Zhang" in outcomes[0][0] and "(owner)" in outcomes[0][0]


class TestSourceAgnostic:
    """The hint path never looks at which transport wrote the raw file."""

    def test_remember_write_raw_and_auto_memory_path_raw_route_to_owner(
        self, tmp_path: Path
    ) -> None:
        wiki, owner_page, _, registry = _corpus(tmp_path)
        raw_root = tmp_path / "knowledge" / "raw"
        # stdio and HTTP both write through ``remember_write``; the session
        # subdirectory is the only difference between them.
        remember_write(raw_root, "I always want release notes in bullet form.", "claude-session")
        remember_write(raw_root, "I never want release notes in prose.", "remote-session")
        from athenaeum.intake import discover_raw_files

        files = discover_raw_files(raw_root)
        assert len(files) == 2
        auto = _raw(
            raw_root / "auto-memory" / "scope" / "feedback_notes.md",
            "---\nname: feedback_notes\ntype: feedback\n---\nI like terse commit messages.\n",
        )
        for i, raw in enumerate([*files, auto]):
            claim = f"Preference: item {i}."
            client = _Queued(
                [
                    json.dumps([{"candidate_uid": OWNER_UID, "observations": claim}]),
                    _ops(f"## Preferences\n\n- item {i}."),
                ]
            )
            _run_sync(wiki, raw, client, CONFIG, registry)
            assert f"- item {i}." in owner_page.read_text(encoding="utf-8")
