# SPDX-License-Identifier: Apache-2.0
"""Tests for wiring sensitivity routing into the librarian raw sweep (athenaeum#1025).

Slice 4/4 of athenaeum#949's design note (`docs/sensitivity-value-routing.md`)
— the actual raw-sweep hook: :func:`athenaeum.sensitivity_routing.route_sensitive_values`
(slices 2/3, athenaeum#1023/athenaeum#1024) called at the top of
:func:`athenaeum.librarian.process_one`, before Tier 0's passthrough write
and before Tier 1/2/3 read ``raw.content`` at all.

Mirrors ``tests/test_screening.py``'s ``process_one``-level coverage style
per the issue's own "Plan" section. Deliberately out of scope here (per the
issue): the routing/redaction mechanism's own unit tests (slice 2,
``tests/test_sensitivity_value_routing.py::TestRouteSensitiveValues``) and
the record-keyed read path's own unit tests (slice 3, same file's
``TestResolveSensitiveRecord``) — this file only tests that the already-
tested mechanism is wired in at the right place, with the right scope
(body only), and with the right failure/idempotency posture at the
``process_one`` integration level.

All values below are synthetic — nothing here ever touches real PII.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from athenaeum.librarian import process_one
from athenaeum.models import EntityIndex, RawFile, parse_frontmatter

SYNTHETIC_EMAIL = "test.user.demo@example.invalid"
ROUTING_ON = {"sensitivity": {"routing": {"enabled": True}}}
#: AC10 failure mode 3 (design note §6): routes the `pii` class onto an
#: IN-CORPUS adapter — a misconfiguration the routing stage must refuse
#: rather than silently honor.
ROUTING_ON_UNSAFE_MAPPING = {
    "sensitivity": {"routing": {"enabled": True}},
    "storage": {"mapping": {"pii": "wiki-markdown-embedded"}},
}


def _raw_file(raw_dir: Path, filename: str, content: str, *, source: str = "sessions") -> RawFile:
    raw_dir.mkdir(parents=True, exist_ok=True)
    path = raw_dir / filename
    path.write_text(content, encoding="utf-8")
    timestamp, _, uuid8_md = filename.partition("-")
    uuid8 = uuid8_md.removesuffix(".md")
    return RawFile(path=path, source=source, timestamp=timestamp, uuid8=uuid8)


def _vault_files(knowledge_root: Path, sensitivity_class: str = "pii") -> list[Path]:
    return list((knowledge_root / "excluded" / "sensitivity" / sensitivity_class).glob("*.md"))


class TestTier0PassthroughRouting:
    def test_body_match_compiles_to_pointer_with_frontmatter_intact(
        self, tmp_path: Path
    ) -> None:
        """AC (design note §4/AC6): a pre-structured raw file (Tier 0
        passthrough path) whose body matches a routed sensitivity class
        compiles to a wiki page containing the pointer, not the value, with
        its frontmatter block intact and parseable."""
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        raw = _raw_file(
            tmp_path / "raw" / "sessions",
            "20260810T090000Z-aaaaaaaa.md",
            (
                "---\n"
                "uid: note-1\n"
                "type: reference\n"
                "name: Support ticket\n"
                "access: internal\n"
                "tags:\n"
                "  - support\n"
                "---\n\n"
                f"Contact {SYNTHETIC_EMAIL} for follow-up.\n"
            ),
        )

        client = MagicMock()
        client.messages.create.side_effect = AssertionError(
            "Tier 0 passthrough must not reach the LLM tiers"
        )
        result = process_one(
            raw,
            EntityIndex(wiki_root),
            wiki_root,
            client,
            valid_types=["reference"],
            valid_tags=["support"],
            valid_access=["open", "internal", "confidential", "personal"],
            config=ROUTING_ON,
        )
        client.messages.create.assert_not_called()
        assert result.created, "expected a wiki page to be created via Tier 0 passthrough"

        pages = list(wiki_root.glob("*.md"))
        assert len(pages) == 1
        written = pages[0].read_text(encoding="utf-8")
        meta, body = parse_frontmatter(written)

        # Frontmatter block intact and parseable — untouched by redaction.
        assert meta.get("uid") == "note-1"
        assert meta.get("type") == "reference"
        assert meta.get("name") == "Support ticket"
        assert meta.get("access") == "internal"
        assert meta.get("tags") == ["support"]

        # Body carries the pointer, never the value.
        assert SYNTHETIC_EMAIL not in written
        assert "[sensitive:pii:" in body

        # The value landed in the vault, resolvable — not dropped, not
        # left in the corpus.
        vault_files = _vault_files(tmp_path)
        assert len(vault_files) == 1
        assert SYNTHETIC_EMAIL in vault_files[0].read_text(encoding="utf-8")


class TestTier23UnstructuredRouting:
    def test_value_never_reaches_the_llm_request_payload(self, tmp_path: Path) -> None:
        """AC (design note §4/AC6): an unstructured raw file (Tier 2/3
        path) whose body matches a routed sensitivity class compiles
        without the value appearing anywhere in the mocked LLM call's OWN
        request payload — inspected directly, not merely the resulting
        wiki page."""
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        raw = _raw_file(
            tmp_path / "raw" / "sessions",
            "20260810T091500Z-bbbbbbbb.md",
            f"Reach {SYNTHETIC_EMAIL} about the invoice.\n",
        )

        classify_response = MagicMock()
        classify_response.content = [
            MagicMock(
                text=json.dumps(
                    [
                        {
                            "name": "Invoice contact",
                            "entity_type": "reference",
                            "tags": [],
                            "access": "internal",
                            "observations": "Follow up regarding an outstanding invoice.",
                        }
                    ]
                )
            )
        ]
        create_response = MagicMock()
        create_response.content = [
            MagicMock(
                text="# Invoice contact\n\nFollow up regarding an outstanding invoice.\n"
            )
        ]
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = [classify_response, create_response]

        result = process_one(
            raw,
            EntityIndex(wiki_root),
            wiki_root,
            mock_client,
            valid_types=["reference"],
            valid_tags=[],
            valid_access=["open", "internal", "confidential", "personal"],
            config=ROUTING_ON,
        )
        assert result.created

        assert mock_client.messages.create.call_count == 2
        for call in mock_client.messages.create.call_args_list:
            assert SYNTHETIC_EMAIL not in str(call.kwargs)

        # Tier 2's classify prompt (the fenced `raw.content`) carries the
        # pointer instead of the raw value — the design note's own
        # spike-verification claim, checked directly here.
        classify_call = mock_client.messages.create.call_args_list[0]
        assert "[sensitive:pii:" in str(classify_call.kwargs)

        pages = list(wiki_root.glob("*.md"))
        assert len(pages) == 1
        assert SYNTHETIC_EMAIL not in pages[0].read_text(encoding="utf-8")

        vault_files = _vault_files(tmp_path)
        assert len(vault_files) == 1


class TestFailClosed:
    def test_routing_failure_propagates_uncaught_and_leaves_no_trace(
        self, tmp_path: Path
    ) -> None:
        """AC (design note §6/AC10): a routing failure propagates out of
        ``process_one`` uncaught — exercised at THIS integration level, not
        just inside ``route_sensitive_values`` itself. The raw file is left
        on disk untouched and no wiki page is written for it. The existing
        entity-tier sweep loop's generic exception handling is not modified
        by this slice, so this test asserts on the propagating exception
        directly rather than on any sweep-loop behavior."""
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        original_content = (
            "---\n"
            "uid: note-2\n"
            "type: reference\n"
            "name: Unsafe routing target\n"
            "access: internal\n"
            "---\n\n"
            f"Contact {SYNTHETIC_EMAIL} for follow-up.\n"
        )
        raw = _raw_file(
            tmp_path / "raw" / "sessions",
            "20260810T093000Z-cccccccc.md",
            original_content,
        )

        client = MagicMock()
        client.messages.create.side_effect = AssertionError(
            "a routing failure must fail BEFORE any LLM tier runs"
        )

        from athenaeum.sensitivity_routing import SensitivityRoutingError

        with pytest.raises(SensitivityRoutingError) as excinfo:
            process_one(
                raw,
                EntityIndex(wiki_root),
                wiki_root,
                client,
                valid_types=["reference"],
                valid_tags=[],
                valid_access=["open", "internal", "confidential", "personal"],
                config=ROUTING_ON_UNSAFE_MAPPING,
            )
        assert SYNTHETIC_EMAIL not in str(excinfo.value)
        client.messages.create.assert_not_called()

        # Raw file left on disk, byte-for-byte untouched.
        assert raw.path.read_text(encoding="utf-8") == original_content
        # No wiki page written for it.
        assert not any(wiki_root.glob("*.md"))
        # Nothing landed in the vault either — the failure is fully atomic.
        assert not (tmp_path / "excluded").exists() or not any(
            (tmp_path / "excluded").rglob("*.md")
        )


class TestDisabledByDefault:
    def test_routing_unset_is_unchanged_from_pre_949_behavior(self, tmp_path: Path) -> None:
        """AC (issue athenaeum#1025): with ``sensitivity.routing.enabled``
        unset (the default), a raw file's ``process_one`` output is
        unchanged from pre-athenaeum#949 behavior — the value compiles
        through verbatim, exactly as it did before this stage existed."""
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        raw = _raw_file(
            tmp_path / "raw" / "sessions",
            "20260810T094500Z-dddddddd.md",
            (
                "---\n"
                "uid: note-3\n"
                "type: reference\n"
                "name: Unrouted note\n"
                "access: internal\n"
                "---\n\n"
                f"Contact {SYNTHETIC_EMAIL} for follow-up.\n"
            ),
        )

        client = MagicMock()
        client.messages.create.side_effect = AssertionError(
            "Tier 0 passthrough must not reach the LLM tiers"
        )
        # config=None is the default every legacy caller passes.
        result = process_one(
            raw,
            EntityIndex(wiki_root),
            wiki_root,
            client,
            valid_types=["reference"],
            valid_tags=[],
            valid_access=["open", "internal", "confidential", "personal"],
            config=None,
        )
        assert result.created

        pages = list(wiki_root.glob("*.md"))
        assert len(pages) == 1
        # Unchanged: the raw value compiles through verbatim.
        assert SYNTHETIC_EMAIL in pages[0].read_text(encoding="utf-8")
        # No vault surface is even created when routing never fires.
        assert not (tmp_path / "excluded").exists()


class TestIdempotentReentrancy:
    def test_repeated_sweep_over_same_raw_does_not_duplicate_the_vault_record(
        self, tmp_path: Path
    ) -> None:
        """AC (design note §7.1/AC11): the sweep runs nightly over
        append-only raw — re-screening an already-routed raw file (a fresh
        ``RawFile`` re-discovering the SAME on-disk content, exactly as a
        second nightly sweep would) must not create a duplicate vault
        record."""
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        raw_dir = tmp_path / "raw" / "sessions"
        filename = "20260810T100000Z-eeeeeeee.md"
        raw_dir.mkdir(parents=True)
        (raw_dir / filename).write_text(
            f"Reach {SYNTHETIC_EMAIL} for a quote.\n", encoding="utf-8"
        )

        # Tier 2 finds nothing (an empty classification) both runs, so no
        # wiki write or Tier 3 call happens either time — isolates the
        # routing hook's own re-entrancy from Tier 0 index/uid concerns.
        classify_response = MagicMock()
        classify_response.content = [MagicMock(text=json.dumps([]))]
        mock_client = MagicMock()
        mock_client.messages.create.return_value = classify_response

        for _ in range(2):
            raw = RawFile(
                path=raw_dir / filename,
                source="sessions",
                timestamp="20260810T100000Z",
                uuid8="eeeeeeee",
            )
            process_one(
                raw,
                EntityIndex(wiki_root),
                wiki_root,
                mock_client,
                valid_types=["reference"],
                valid_tags=[],
                valid_access=["internal"],
                config=ROUTING_ON,
            )

        vault_files = _vault_files(tmp_path)
        assert len(vault_files) == 1, (
            "re-sweeping the same raw content must not duplicate the vault record"
        )
