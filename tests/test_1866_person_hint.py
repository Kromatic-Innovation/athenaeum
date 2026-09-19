# SPDX-License-Identifier: Apache-2.0
"""Tests for issue athenaeum#1866 — the tier-0 person-registry consult becomes
a HINT for the reasoning tiers, not a whole-file claim.

Companion to the tiers.py-level unit tests in
``tests/test_tiers.py::TestPersonHintClassifyPrompt`` /
``TestPersonHintClassifyParsing`` / ``TestPersonHintMerge`` (the request
shape, the parse drop rules, and the merge verify decisions). This file
drives ``athenaeum.librarian.process_one`` directly (not the full
``librarian.run()`` pipeline — no audit-on-touch, no git snapshot, faster
and narrower) to pin the PIPELINE-level acceptance criteria: a hinted file
reaches tier 2 and processes the rest of the file normally (AC1), a
tier1-matched hint produces no unconditional ``raw.content[:2000]`` update
(AC5), an unaffirmed candidate gets no write-model call and no page change
(AC6/AC2), the hint-derived-action fan-out cap (AC8), and the per-candidate
decision ledger (AC9).

All fixtures are synthetic — no client data lives in this public repo.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from athenaeum.intake import PERSON_OBSERVATION_MAX_FANOUT
from athenaeum.librarian import process_one
from athenaeum.models import EntityIndex, RawFile, TokenUsage
from athenaeum.person_registry import PersonRegistry


def _write_person(root: Path, *, uid: str, name: str, body: str = "Body.\n") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    filename = f"{uid}-{name.lower().replace(' ', '-')}.md"
    path = root / filename
    path.write_text(
        f"---\nuid: {uid}\ntype: person\nname: {name}\n---\n\n# {name}\n\n{body}",
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


class _QueuedClient:
    """Records every call and returns canned responses in order.

    Raises ``AssertionError`` (not a bare ``StopIteration`` — see the
    exhausted-queue-diagnosis retro this convention exists to sidestep) if
    ``process_one`` asks for more calls than the test queued — a clearer
    failure than a random-looking downstream exception.
    """

    def __init__(self, responses: list[tuple[str, str]]) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []
        self.messages = self

    def create(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        if not self._responses:
            raise AssertionError(
                f"no more queued responses (call #{len(self.calls)})"
            )
        text, stop_reason = self._responses.pop(0)
        response = MagicMock()
        response.content = [MagicMock(text=text)]
        response.stop_reason = stop_reason
        return response


_VALID_TYPES = ["person", "company"]
_VALID_TAGS = ["active"]
_VALID_ACCESS = ["internal"]


class TestAC1HintNeverClaimsTheFileWhole:
    def test_person_mention_reaches_tier2_and_other_entity_is_created_same_run(
        self, tmp_path: Path
    ) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        _write_person(wiki, uid="person1a", name="Alice Zhang")
        index = EntityIndex(wiki)
        registry = PersonRegistry(wiki)

        raw = _make_raw(
            "Caught up with Alice Zhang today. She mentioned Acme Corp is "
            "raising a Series C round."
        )
        classify_text = json.dumps(
            [
                {
                    "candidate_uid": "person1a",
                    "observations": "Mentioned Acme Corp is raising a Series C round.",
                },
                {
                    "name": "Acme Corp",
                    "entity_type": "company",
                    "tags": [],
                    "access": "internal",
                    "observations": "Raising a Series C round.",
                },
            ]
        )
        merge_text = json.dumps(
            {
                "ops": [{"op": "append_section", "text": "- Raising a Series C round."}],
                "adds_new_claim": True,
            }
        )
        create_text = "# Acme Corp\n\nRaising a Series C round."
        client = _QueuedClient(
            [
                (classify_text, "end_turn"),
                (merge_text, "end_turn"),
                (create_text, "end_turn"),
            ]
        )

        result = process_one(
            raw,
            index,
            wiki,
            client,
            _VALID_TYPES,
            _VALID_TAGS,
            _VALID_ACCESS,
            person_registry=registry,
        )

        # The mention reached tier 2 (not a zero-call whole-file claim), and
        # the OTHER entity's create call also happened (not just the hint).
        assert len(client.calls) == 3
        # The OTHER entity in the same file was created in the SAME run —
        # the whole point of no longer claiming the file whole.
        assert [e.name for e in result.created] == ["Acme Corp"]
        assert result.updated == ["person1a"]


class TestAC5TierOneMatchedHintHasNoUnconditionalUpdate:
    def test_tier1_matched_hint_produces_only_the_hint_derived_action(
        self, tmp_path: Path
    ) -> None:
        """A person tier1 ALSO matches (exact name/alias hit) must not
        additionally produce the unconditional ``raw.content[:2000]``
        update tier1 hits normally build — only the classify-affirmed
        hint-derived update (or none) may touch that uid."""
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        _write_person(wiki, uid="person1a", name="Alice Zhang")
        index = EntityIndex(wiki)
        registry = PersonRegistry(wiki)

        raw = _make_raw("Alice Zhang shipped the new onboarding flow this week.")
        classify_text = json.dumps(
            [
                {
                    "candidate_uid": "person1a",
                    "observations": "Shipped the new onboarding flow.",
                }
            ]
        )
        merge_text = json.dumps(
            {
                "ops": [{"op": "append_section", "text": "- Shipped onboarding."}],
                "adds_new_claim": True,
            }
        )
        client = _QueuedClient([(classify_text, "end_turn"), (merge_text, "end_turn")])

        result = process_one(
            raw,
            index,
            wiki,
            client,
            _VALID_TYPES,
            _VALID_TAGS,
            _VALID_ACCESS,
            person_registry=registry,
        )

        # Exactly one update to this uid — from the hint-derived action, not
        # doubled by an unconditional tier1 update.
        assert result.updated == ["person1a"]
        page_text = (wiki / "person1a-alice-zhang.md").read_text(encoding="utf-8")
        # The whole raw body must never have been pasted verbatim.
        assert "Alice Zhang shipped the new onboarding flow this week." not in page_text


class TestAC6UnaffirmedCandidateGetsNoWriteCallAndNoPageChange:
    def test_classifier_silence_means_no_merge_call_and_byte_identical_page(
        self, tmp_path: Path
    ) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        page = _write_person(
            wiki,
            uid="person1a",
            name="Alice Zhang",
            body="# Alice Zhang\n\n## Notes\n\n- 2026-01-01: old note.\n",
        )
        before = page.read_text(encoding="utf-8")
        index = EntityIndex(wiki)
        registry = PersonRegistry(wiki)

        raw = _make_raw("Talked to Alice Zhang briefly in the hallway.")
        # Classifier affirms NOTHING — a passing mention.
        classify_text = json.dumps([])
        client = _QueuedClient([(classify_text, "end_turn")])

        result = process_one(
            raw,
            index,
            wiki,
            client,
            _VALID_TYPES,
            _VALID_TAGS,
            _VALID_ACCESS,
            person_registry=registry,
        )

        # Only the ONE classify call — no write-model call at all.
        assert len(client.calls) == 1
        assert result.updated == []
        after = page.read_text(encoding="utf-8")
        assert after == before  # byte-identical
        # AC2: no code path appends a dated Notes bullet.
        assert "(source:" not in after

    def test_person_hint_decisions_records_not_asserted(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        _write_person(wiki, uid="person1a", name="Alice Zhang")
        index = EntityIndex(wiki)
        registry = PersonRegistry(wiki)

        raw = _make_raw("Talked to Alice Zhang briefly in the hallway.")
        client = _QueuedClient([(json.dumps([]), "end_turn")])

        result = process_one(
            raw,
            index,
            wiki,
            client,
            _VALID_TYPES,
            _VALID_TAGS,
            _VALID_ACCESS,
            person_registry=registry,
        )

        assert result.person_hint_decisions == [
            ("person1a", "classify", "not_asserted")
        ]


class TestAC8HintActionFanoutCap:
    def test_more_hinted_claims_than_the_fanout_cap_are_capped_and_logged(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()

        n = PERSON_OBSERVATION_MAX_FANOUT + 2
        mentions = []
        classify_items = []
        for i in range(n):
            uid = f"person{i:02d}"
            name = f"Zeta Testperson{i:02d}"
            _write_person(wiki, uid=uid, name=name)
            mentions.append(f"{name} joined the call.")
            classify_items.append(
                {"candidate_uid": uid, "observations": f"{name} joined the call."}
            )
        # Built AFTER every person page is written to disk — EntityIndex
        # (which tier3_derive_actions's update branch resolves the write
        # target through, via ``index.get_by_uid``) snapshots the wiki at
        # construction time.
        index = EntityIndex(wiki)
        registry = PersonRegistry(wiki)

        raw = _make_raw(" ".join(mentions))
        classify_text = json.dumps(classify_items)
        merge_text = json.dumps(
            {
                "ops": [{"op": "append_section", "text": "- joined the call."}],
                "adds_new_claim": True,
            }
        )
        # One merge response per action actually built (capped at
        # PERSON_OBSERVATION_MAX_FANOUT), plus the one classify call.
        responses = [(classify_text, "end_turn")] + [
            (merge_text, "end_turn") for _ in range(PERSON_OBSERVATION_MAX_FANOUT)
        ]
        client = _QueuedClient(responses)

        usage = TokenUsage()
        with caplog.at_level(logging.WARNING):
            result = process_one(
                raw,
                index,
                wiki,
                client,
                _VALID_TYPES,
                _VALID_TAGS,
                _VALID_ACCESS,
                person_registry=registry,
                usage=usage,
            )

        assert len(result.updated) == PERSON_OBSERVATION_MAX_FANOUT
        assert "person-hint action cap" in caplog.text
        # The overflow candidates are recorded as dropped, not silently lost.
        dropped = [d for d in result.person_hint_decisions if d[2] == "dropped"]
        assert len(dropped) == n - PERSON_OBSERVATION_MAX_FANOUT


class TestAC9PersonHintDecisionsLedger:
    def test_decisions_cover_merged_and_not_asserted_candidates(
        self, tmp_path: Path
    ) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        _write_person(wiki, uid="person1a", name="Alice Zhang")
        _write_person(wiki, uid="person2b", name="Bob Diaz")
        index = EntityIndex(wiki)
        registry = PersonRegistry(wiki)

        raw = _make_raw(
            "Alice Zhang shipped onboarding. Bob Diaz was on the call too."
        )
        classify_text = json.dumps(
            [
                {
                    "candidate_uid": "person1a",
                    "observations": "Shipped onboarding.",
                }
            ]
        )
        merge_text = json.dumps(
            {
                "ops": [{"op": "append_section", "text": "- Shipped onboarding."}],
                "adds_new_claim": True,
            }
        )
        client = _QueuedClient([(classify_text, "end_turn"), (merge_text, "end_turn")])

        result = process_one(
            raw,
            index,
            wiki,
            client,
            _VALID_TYPES,
            _VALID_TAGS,
            _VALID_ACCESS,
            person_registry=registry,
            usage=TokenUsage(),
        )

        decisions = {
            uid: (tier, verdict) for uid, tier, verdict in result.person_hint_decisions
        }
        assert decisions["person1a"] == ("write_merge", "merged")
        assert decisions["person2b"] == ("classify", "not_asserted")
