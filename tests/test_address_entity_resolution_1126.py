# SPDX-License-Identifier: Apache-2.0
"""Tier-2 classification must not mint a NEW entity named after a bare email
address (issue athenaeum#1126).

Before this fix, an intake statement whose subject was a bare email address
was classified as a NEW person entity NAMED AFTER that address, minting an
orphan wiki page nothing reads by address and putting a raw address into a
wiki page's ``name:``/title/filename (the standing ``wiki-contacts-no-email``
violation). :func:`athenaeum.tiers.resolve_address_named_classifications`
closes this by resolving an address-shaped classification through the
sanctioned reverse lookup (:func:`athenaeum.identity_resolution.resolve_handle_query`)
or DECLINING it (paired with a Tier-4 escalation so the fact survives) —
never minting the address-named page.

One test class per group:

- ``TestDirectPath`` — AC1/AC2, the AC5 regression guard: an address carried
  on a real excluded contact record resolves to that person, never becomes
  its own page.
- ``TestDecline`` — AC3, both decline reasons (unresolvable address,
  ambiguous multi-address subject).
- ``TestFastPathIsANoOp`` — the fast path performs NO lookup for an ordinary
  (non-address) classification.
- ``TestAC4NoAddressNamedNewEntity`` — no ``kept`` member with ``is_new`` true
  ever carries an email-shaped name.
- ``TestSyncTransportWiring`` — through ``librarian.process_one``: resolved
  addresses become ``update`` actions, declined addresses escalate (even as
  the file's only classification, the empty-``actions`` early-return case).
- ``TestBatchTransportParity`` — the same two outcomes through
  ``batch.process_batch_run``, including the ``st.done`` early-unlink branch.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable
from unittest.mock import MagicMock

import pytest

from athenaeum import identity_resolution, pii
from athenaeum.batch import process_batch_run
from athenaeum.librarian import process_one
from athenaeum.models import ClassifiedEntity, EntityIndex, RawFile, TokenUsage
from athenaeum.tiers import DEFAULT_CLASSIFY_MODEL, resolve_address_named_classifications
from tests.conftest import FakeLLMClient

EXCLUDED_CONFIG: dict[str, object] = {"storage": {"mapping": {"pii": "excluded"}}}

VALID_TYPES = ["person", "company", "concept", "reference"]
VALID_ACCESS = ["open", "internal", "confidential", "personal"]


# ---------------------------------------------------------------------------
# Fixture helpers — copied shape from tests/test_recall_identity_resolution.py
# (this issue's precedent for building a knowledge root + wiki + excluded
# contacts surface); no new fixture scaffold invented.
# ---------------------------------------------------------------------------


def _write_page(
    wiki_root: Path,
    uid: str,
    *,
    name: str,
    entity_type: str = "person",
    extra: str = "",
) -> Path:
    wiki_root.mkdir(parents=True, exist_ok=True)
    path = wiki_root / f"{uid}.md"
    path.write_text(
        f"---\nuid: {uid}\nname: {name}\ntype: {entity_type}\n{extra}---\n\n"
        f"{name} works on widget calibration.\n",
        encoding="utf-8",
    )
    return path


def _write_record(
    root: Path,
    filename: str,
    *,
    uid: str | None,
    fields: str,
) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / filename
    uid_line = f"uid: {uid}\n" if uid is not None else ""
    path.write_text(
        f"---\n{uid_line}pii: true\n{fields}---\n\nArchival data.\n",
        encoding="utf-8",
    )
    return path


def _classified(name: str, *, observations: str = "") -> ClassifiedEntity:
    return ClassifiedEntity(
        name=name,
        entity_type="person",
        tags=[],
        access="internal",
        is_new=True,
        observations=observations,
    )


def _raw(raw_dir: Path, content: str, filename: str = "note.md") -> RawFile:
    raw_dir.mkdir(parents=True, exist_ok=True)
    path = raw_dir / filename
    path.write_text(content, encoding="utf-8")
    return RawFile(path=path, source=raw_dir.name, timestamp="", uuid8="")


# ---------------------------------------------------------------------------
# AC1/AC2 — the direct path (AC5 regression guard)
# ---------------------------------------------------------------------------


class TestDirectPath:
    def test_resolves_to_the_owning_person_never_a_new_page(self, tmp_path: Path) -> None:
        knowledge = tmp_path / "knowledge"
        _write_page(knowledge / "wiki", "alex", name="Alex Widget")
        _write_record(
            pii.contacts_surface_root(knowledge, EXCLUDED_CONFIG),
            "alex-contact.md",
            uid="alex",
            fields="emails:\n  - alex@example.org\n",
        )

        classified = [_classified("alex@example.org")]
        outcome = resolve_address_named_classifications(
            classified,
            knowledge_root=knowledge,
            wiki_root=knowledge / "wiki",
            config=EXCLUDED_CONFIG,
        )

        assert len(outcome.kept) == 1
        resolved_entity = outcome.kept[0]
        assert resolved_entity.is_new is False
        assert resolved_entity.existing_uid == "alex"
        assert resolved_entity.name == "Alex Widget"
        assert outcome.declined == ()
        assert outcome.resolved == (("alex@example.org", "alex", "Alex Widget"),)
        # No address-named entity anywhere in the outcome.
        assert not identity_resolution.carries_email_shape(resolved_entity.name)


# ---------------------------------------------------------------------------
# AC3 — decline
# ---------------------------------------------------------------------------


class TestDecline:
    def test_unresolvable_address_declines_no_match(self, tmp_path: Path) -> None:
        knowledge = tmp_path / "knowledge"
        (knowledge / "wiki").mkdir(parents=True)

        classified = [_classified("ghost@example.com")]
        outcome = resolve_address_named_classifications(
            classified,
            knowledge_root=knowledge,
            wiki_root=knowledge / "wiki",
            config=EXCLUDED_CONFIG,
        )

        assert outcome.kept == []
        assert outcome.declined == (("ghost@example.com", "no-match"),)
        assert outcome.resolved == ()

    def test_ambiguous_multi_address_subject_declines(self, tmp_path: Path) -> None:
        knowledge = tmp_path / "knowledge"
        (knowledge / "wiki").mkdir(parents=True)

        name = "Contact a@example.com or b@example.com"
        classified = [_classified(name)]
        outcome = resolve_address_named_classifications(
            classified,
            knowledge_root=knowledge,
            wiki_root=knowledge / "wiki",
            config=EXCLUDED_CONFIG,
        )

        assert outcome.kept == []
        assert outcome.declined == ((name, "ambiguous-subject"),)


# ---------------------------------------------------------------------------
# No-op fast path — no lookup of any kind for a non-address classification
# ---------------------------------------------------------------------------


class TestFastPathIsANoOp:
    def test_ordinary_classification_unchanged_and_no_lookup(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _must_not_be_called(*_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError(
                "resolve_handle_query must not be called on the fast path (athenaeum#1126)"
            )

        monkeypatch.setattr(identity_resolution, "resolve_handle_query", _must_not_be_called)

        knowledge = tmp_path / "knowledge"
        classified = [_classified("Ada Lovelace")]
        outcome = resolve_address_named_classifications(
            classified,
            knowledge_root=knowledge,
            wiki_root=knowledge / "wiki",
            config=EXCLUDED_CONFIG,
        )

        assert outcome.kept == classified
        assert outcome.kept[0].name == "Ada Lovelace"
        assert outcome.kept[0].is_new is True
        assert outcome.declined == ()
        assert outcome.resolved == ()


# ---------------------------------------------------------------------------
# AC4 — no address ever survives as a NEW entity's name
# ---------------------------------------------------------------------------


class TestAC4NoAddressNamedNewEntity:
    def test_mixed_corpus_never_keeps_a_new_address_named_entity(self, tmp_path: Path) -> None:
        knowledge = tmp_path / "knowledge"
        _write_page(knowledge / "wiki", "alex", name="Alex Widget")
        _write_record(
            pii.contacts_surface_root(knowledge, EXCLUDED_CONFIG),
            "alex-contact.md",
            uid="alex",
            fields="emails:\n  - alex@example.org\n",
        )

        classified = [
            _classified("alex@example.org"),  # resolves
            _classified("ghost@example.com"),  # declines: no-match
            _classified("a@example.com or b@example.com"),  # declines: ambiguous
            _classified("Ada Lovelace"),  # ordinary, unaffected
        ]
        outcome = resolve_address_named_classifications(
            classified,
            knowledge_root=knowledge,
            wiki_root=knowledge / "wiki",
            config=EXCLUDED_CONFIG,
        )

        for c in outcome.kept:
            if c.is_new:
                assert not identity_resolution.carries_email_shape(c.name), c.name
        # Only the resolved address and the ordinary name survive.
        assert {c.name for c in outcome.kept} == {"Alex Widget", "Ada Lovelace"}


# ---------------------------------------------------------------------------
# Sync-transport wiring — librarian.process_one
# ---------------------------------------------------------------------------


class TestSyncTransportWiring:
    def test_resolved_address_produces_update_not_create(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        knowledge = tmp_path / "knowledge"
        wiki = knowledge / "wiki"
        _write_page(wiki, "alex", name="Alex Widget")
        _write_record(
            pii.contacts_surface_root(knowledge, EXCLUDED_CONFIG),
            "alex-contact.md",
            uid="alex",
            fields="emails:\n  - alex@example.org\n",
        )
        raw = _raw(
            knowledge / "raw" / "producer",
            "Saw alex@example.org at the widget calibration meeting.\n",
        )

        def _fake_tier2_classify(*_args: Any, **_kwargs: Any) -> list[ClassifiedEntity]:
            return [
                _classified(
                    "alex@example.org",
                    observations="Saw alex@example.org at the widget calibration meeting.",
                )
            ]

        monkeypatch.setattr("athenaeum.librarian.tier2_classify", _fake_tier2_classify)

        write_client = FakeLLMClient(
            text=json.dumps(
                {"ops": [{"op": "append_section", "text": "Seen at calibration meeting."}]}
            )
        )
        classify_client = MagicMock()
        classify_client.messages.create.side_effect = AssertionError(
            "tier2_classify is monkeypatched — the real classify client must never be called"
        )

        result = process_one(
            raw,
            EntityIndex(wiki),
            wiki,
            classify_client,
            valid_types=VALID_TYPES,
            valid_tags=[],
            valid_access=VALID_ACCESS,
            config=EXCLUDED_CONFIG,
            write_client=write_client,
        )

        assert result.created == []
        assert result.updated == ["alex"]
        assert result.escalated == []

        page_text = (wiki / "alex.md").read_text(encoding="utf-8")
        assert "Seen at calibration meeting." in page_text
        # No address-named page was ever created.
        assert not any(identity_resolution.carries_email_shape(p.stem) for p in wiki.glob("*.md"))
        assert sorted(p.name for p in wiki.glob("*.md")) == ["alex.md"]

    def test_declined_address_escalates_even_as_the_only_classification(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        knowledge = tmp_path / "knowledge"
        wiki = knowledge / "wiki"
        wiki.mkdir(parents=True)
        raw = _raw(
            knowledge / "raw" / "producer",
            "A note about ghost@example.com with no other subject.\n",
        )

        def _fake_tier2_classify(*_args: Any, **_kwargs: Any) -> list[ClassifiedEntity]:
            return [
                _classified(
                    "ghost@example.com",
                    observations="A note about ghost@example.com with no other subject.",
                )
            ]

        monkeypatch.setattr("athenaeum.librarian.tier2_classify", _fake_tier2_classify)

        classify_client = MagicMock()
        classify_client.messages.create.side_effect = AssertionError(
            "tier2_classify is monkeypatched — the real classify client must never be called"
        )
        write_client = MagicMock()
        write_client.messages.create.side_effect = AssertionError(
            "No actions survive the address-resolution gate here — tier-3 "
            "must never be called (athenaeum#1126 empty-actions early return)"
        )

        result = process_one(
            raw,
            EntityIndex(wiki),
            wiki,
            classify_client,
            valid_types=VALID_TYPES,
            valid_tags=[],
            valid_access=VALID_ACCESS,
            config=EXCLUDED_CONFIG,
            write_client=write_client,
        )

        assert result.created == []
        assert result.updated == []
        assert len(result.escalated) == 1

        pending = (wiki / "_pending_questions.md").read_text(encoding="utf-8")
        assert "ghost@example.com" in pending
        assert "no other subject" in pending  # the raw statement text survives
        # No wiki page whose filename or `name:` carries an address.
        pages = list(wiki.glob("*.md"))
        assert all(p.name == "_pending_questions.md" for p in pages)


# ---------------------------------------------------------------------------
# Batch-transport parity — batch.process_batch_run
# ---------------------------------------------------------------------------


class _FakeBatches:
    """Minimal ``client.messages.batches`` double — every batch ends
    immediately (no polling), results keyed off the request content via
    *responder*. Trimmed from ``tests/test_batch_mode.py``'s fuller double
    (no failure/truncation variants — not needed here)."""

    def __init__(self, responder: Callable[[dict[str, Any]], str]) -> None:
        self._responder = responder
        self.submitted: list[list[dict[str, Any]]] = []

    def create(self, *, requests: list[dict[str, Any]]) -> SimpleNamespace:
        requests = list(requests)
        self.submitted.append(requests)
        return SimpleNamespace(id=f"batch_{len(self.submitted)}", processing_status="ended")

    def retrieve(self, batch_id: str) -> SimpleNamespace:
        return SimpleNamespace(id=batch_id, processing_status="ended")

    def results(self, batch_id: str) -> Any:
        idx = int(batch_id.split("_")[1]) - 1
        for req in self.submitted[idx]:
            user_msg = req["params"]["messages"][0]["content"]
            yield SimpleNamespace(
                custom_id=req["custom_id"],
                result=SimpleNamespace(
                    type="succeeded",
                    message=SimpleNamespace(
                        content=[SimpleNamespace(text=self._responder(req["params"]))],
                        stop_reason=None,
                        usage=SimpleNamespace(
                            input_tokens=10,
                            output_tokens=10,
                            cache_creation_input_tokens=0,
                            cache_read_input_tokens=0,
                        ),
                    ),
                ),
            )
            _ = user_msg


def _fake_client(responder: Callable[[dict[str, Any]], str]) -> SimpleNamespace:
    batches = _FakeBatches(responder)

    def _unexpected_sync_create(**_kwargs: Any) -> Any:
        raise AssertionError("unexpected synchronous messages.create in batch mode")

    return SimpleNamespace(
        messages=SimpleNamespace(create=_unexpected_sync_create, batches=batches)
    )


def _classify_or_merge_responder(address_name: str) -> Callable[[dict[str, Any]], str]:
    def _responder(params: dict[str, Any]) -> str:
        user_msg = params["messages"][0]["content"]
        if params.get("model") == DEFAULT_CLASSIFY_MODEL:
            return json.dumps(
                [
                    {
                        "name": address_name,
                        "entity_type": "person",
                        "tags": [],
                        "access": "internal",
                        "observations": user_msg[:200],
                    }
                ]
            )
        if "## Existing page content" in user_msg:
            return json.dumps(
                {"ops": [{"op": "append_section", "text": "Seen at calibration meeting."}]}
            )
        raise AssertionError(f"unrecognized batch request: {user_msg[:120]}")

    return _responder


class TestBatchTransportParity:
    def test_resolved_address_produces_update_not_create(self, tmp_path: Path) -> None:
        knowledge = tmp_path / "knowledge"
        wiki = knowledge / "wiki"
        _write_page(wiki, "alex", name="Alex Widget")
        _write_record(
            pii.contacts_surface_root(knowledge, EXCLUDED_CONFIG),
            "alex-contact.md",
            uid="alex",
            fields="emails:\n  - alex@example.org\n",
        )
        raw = _raw(
            knowledge / "raw" / "producer",
            "Saw alex@example.org at the widget calibration meeting.\n",
        )

        client = _fake_client(_classify_or_merge_responder("alex@example.org"))
        result = process_batch_run(
            [raw],
            EntityIndex(wiki),
            wiki,
            client,
            VALID_TYPES,
            [],
            VALID_ACCESS,
            usage=TokenUsage(),
            config=EXCLUDED_CONFIG,
            max_api_calls=100,
            provider="api",
            sleep=lambda _s: None,
            write_client=client,
        )

        assert result.created == 0
        assert result.updated == 1
        assert result.escalated == 0
        assert not result.failed_refs

        page_text = (wiki / "alex.md").read_text(encoding="utf-8")
        assert "Seen at calibration meeting." in page_text
        assert sorted(p.name for p in wiki.glob("*.md")) == ["alex.md"]
        assert not raw.path.exists()

    def test_declined_address_escalates_on_st_done_early_branch(self, tmp_path: Path) -> None:
        knowledge = tmp_path / "knowledge"
        wiki = knowledge / "wiki"
        wiki.mkdir(parents=True)
        raw = _raw(
            knowledge / "raw" / "producer",
            "A note about ghost@example.com with no other subject.\n",
        )

        client = _fake_client(_classify_or_merge_responder("ghost@example.com"))
        result = process_batch_run(
            [raw],
            EntityIndex(wiki),
            wiki,
            client,
            VALID_TYPES,
            [],
            VALID_ACCESS,
            usage=TokenUsage(),
            config=EXCLUDED_CONFIG,
            max_api_calls=100,
            provider="api",
            sleep=lambda _s: None,
            write_client=client,
        )

        assert result.created == 0
        assert result.updated == 0
        assert result.escalated == 1
        assert not result.failed_refs
        # No tier-3 batch was ever submitted — the file had zero surviving
        # actions after the decline (st.done early branch).
        assert len(client.messages.batches.submitted) == 1

        pending = (wiki / "_pending_questions.md").read_text(encoding="utf-8")
        assert "ghost@example.com" in pending
        assert "no other subject" in pending
        pages = list(wiki.glob("*.md"))
        assert all(p.name == "_pending_questions.md" for p in pages)
        assert not raw.path.exists()
