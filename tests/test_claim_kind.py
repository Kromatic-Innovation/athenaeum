# SPDX-License-Identifier: Apache-2.0
"""Tests for the intake-time claim-kind classifier (issue #327).

Covers:
- ``classify_claim_kind`` with a stubbed LLM returns a valid kind.
- Fail-open: no client, malformed JSON, and out-of-vocabulary labels all
  return ``""`` (unclassified).
- ``stamp_claim_kind`` writes the label into frontmatter, is idempotent, and
  never re-classifies an already-classified file.
- tier0 passthrough round-trips a ``claim_kind`` frontmatter key byte-for-byte.
- ``parse_claim_kind`` fail-open on absent / invalid values.

No network: every "client" is a :class:`unittest.mock.MagicMock` mirroring the
Anthropic SDK shape.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from athenaeum.claim_kind import classify_claim_kind, stamp_claim_kind
from athenaeum.librarian import tier0_passthrough
from athenaeum.models import (
    EntityIndex,
    parse_claim_kind,
    parse_frontmatter,
)


def _client(payload_text: str) -> MagicMock:
    client = MagicMock()
    response = MagicMock()
    response.content = [MagicMock(text=payload_text)]
    response.usage = MagicMock(
        input_tokens=1,
        output_tokens=1,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
    )
    client.messages.create.return_value = response
    return client


# ---------------------------------------------------------------------------
# classify_claim_kind
# ---------------------------------------------------------------------------


class TestClassifyClaimKind:
    def test_valid_opinion_label(self) -> None:
        client = _client('{"claim_kind": "opinion"}')
        assert classify_claim_kind("Tabs are better than spaces.", client) == "opinion"

    def test_valid_fact_label(self) -> None:
        client = _client('prose... {"claim_kind": "fact"} trailing')
        assert classify_claim_kind("The develop tip is abc123.", client) == "fact"

    def test_no_client_is_unclassified(self) -> None:
        assert classify_claim_kind("anything", None) == ""

    def test_out_of_vocabulary_label_fails_open(self) -> None:
        client = _client('{"claim_kind": "vibe"}')
        assert classify_claim_kind("something", client) == ""

    def test_malformed_json_fails_open(self) -> None:
        client = _client("I think this is an opinion, no JSON here.")
        assert classify_claim_kind("something", client) == ""

    def test_api_error_fails_open(self) -> None:
        client = MagicMock()
        client.messages.create.side_effect = RuntimeError("boom")
        assert classify_claim_kind("something", client) == ""

    def test_empty_body_no_call(self) -> None:
        client = _client('{"claim_kind": "opinion"}')
        assert classify_claim_kind("", client) == ""
        client.messages.create.assert_not_called()


# ---------------------------------------------------------------------------
# stamp_claim_kind
# ---------------------------------------------------------------------------


def _write(tmp_path: Path, body: str, *, frontmatter: str = "") -> Path:
    path = tmp_path / "feedback_probe.md"
    fm = f"---\n{frontmatter}---\n" if frontmatter else ""
    path.write_text(fm + body + "\n", encoding="utf-8")
    return path


class TestStampClaimKind:
    def test_stamps_absent_claim_kind(self, tmp_path: Path) -> None:
        path = _write(tmp_path, "The onboarding flow feels clunky.")
        client = _client('{"claim_kind": "opinion"}')
        kind = stamp_claim_kind(path, client)
        assert kind == "opinion"
        meta, _ = parse_frontmatter(path.read_text())
        assert meta.get("claim_kind") == "opinion"

    def test_idempotent_no_reclassify(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path,
            "Tabs beat spaces.",
            frontmatter="claim_kind: opinion\n",
        )
        client = _client('{"claim_kind": "fact"}')  # would change it if it ran
        kind = stamp_claim_kind(path, client)
        assert kind == "opinion"
        client.messages.create.assert_not_called()

    def test_no_client_leaves_unstamped(self, tmp_path: Path) -> None:
        path = _write(tmp_path, "Some claim.")
        assert stamp_claim_kind(path, None) == ""
        meta, _ = parse_frontmatter(path.read_text())
        assert "claim_kind" not in meta

    def test_failed_classification_writes_nothing(self, tmp_path: Path) -> None:
        path = _write(tmp_path, "Some claim.")
        before = path.read_text()
        client = _client("no json")
        assert stamp_claim_kind(path, client) == ""
        assert path.read_text() == before


# ---------------------------------------------------------------------------
# tier0 round-trip + parse_claim_kind fail-open
# ---------------------------------------------------------------------------


class TestClaimKindRoundTrip:
    def test_tier0_passthrough_preserves_claim_kind(self, tmp_path: Path) -> None:
        wiki_root = tmp_path / "wiki"
        wiki_root.mkdir()
        raw = tmp_path / "raw.md"
        raw.write_text(
            "---\n"
            "uid: '12345'\n"
            "type: reference\n"
            "name: Deploy target opinion\n"
            "claim_kind: opinion\n"
            "---\n"
            "Fly.io is the nicer platform.\n",
            encoding="utf-8",
        )
        from athenaeum.models import RawFile

        rf = RawFile(
            path=raw,
            source="sessions",
            timestamp="20240407T120000Z",
            uuid8="aabb0011",
            _content=raw.read_text(encoding="utf-8"),
        )
        index = EntityIndex(wiki_root)
        entity = tier0_passthrough(rf, index, wiki_root, ["reference"])
        assert entity is not None
        written = (wiki_root / entity.filename).read_text(encoding="utf-8")
        meta, _ = parse_frontmatter(written)
        assert meta.get("claim_kind") == "opinion"
        assert parse_claim_kind(meta) == "opinion"

    def test_parse_claim_kind_absent_is_unclassified(self) -> None:
        assert parse_claim_kind(None) == ""
        assert parse_claim_kind({}) == ""
        assert parse_claim_kind({"claim_kind": ""}) == ""

    def test_parse_claim_kind_invalid_fails_open(self) -> None:
        assert parse_claim_kind({"claim_kind": "vibe"}) == ""
        assert parse_claim_kind({"claim_kind": 42}) == ""

    def test_parse_claim_kind_valid(self) -> None:
        for kind in (
            "fact",
            "observation",
            "opinion",
            "decision",
            "policy",
            "definition",
        ):
            assert parse_claim_kind({"claim_kind": kind}) == kind
