# SPDX-License-Identifier: Apache-2.0
"""Tests for `athenaeum.frontmatter_edit` (issue athenaeum#2159).

Two jobs. First, pin that the move out of `repair.py` was a MOVE and not a
copy: `repair`'s private names must BE these objects, because the whole
point of the factoring is that intake and `repair --backfill-sources`
cannot drift. Second, pin the witnessed field mapping — including the one
ref-numbering trap the issue calls out (the memory's `originTurn` is not
the witness ledger's own `turn`/`seq` counter).
"""

from __future__ import annotations

import pytest

from athenaeum import repair
from athenaeum.frontmatter_edit import (
    DEFAULTED_INFERRED_SOURCE,
    WITNESSED_SOURCE_TYPE,
    apply_frontmatter_scalars,
    set_frontmatter_scalar,
    witnessed_source_fields,
    yaml_scalar_literal,
)
from athenaeum.models import SOURCE_TYPES


class TestNoSecondCopy:
    """`repair` must delegate, not transcribe (issue athenaeum#2159)."""

    def test_repair_private_names_are_the_shared_objects(self) -> None:
        assert repair._set_frontmatter_scalar is set_frontmatter_scalar
        assert repair._yaml_scalar_literal is yaml_scalar_literal
        assert repair._DEFAULTED_INFERRED_SOURCE is DEFAULTED_INFERRED_SOURCE

    def test_defaulted_inferred_source_scalar_is_byte_identical(self) -> None:
        # ``mcp_server._DEFAULT_INFERRED_SOURCE`` carries the "KEEP THIS
        # SCALAR BYTE-IDENTICAL" comment and is pinned against `repair`'s
        # alias by tests/test_mcp_server.py; this is the same pin at the
        # scalar's new home.
        assert DEFAULTED_INFERRED_SOURCE == "claude:inferred"

    def test_witnessed_channel_is_in_the_source_type_vocabulary(self) -> None:
        # The literal lives in `frontmatter_edit` so the module stays a leaf
        # (no `models` import); this is the assertion that keeps that
        # trade-off honest.
        assert WITNESSED_SOURCE_TYPE in SOURCE_TYPES


class TestSetFrontmatterScalar:
    def test_replaces_only_the_named_top_level_line(self) -> None:
        fm = "uid: abc\nsource: claude:inferred\nname: thing"
        out = set_frontmatter_scalar(fm, "source", "'witnessed:s1#turn3'")
        assert out == "uid: abc\nsource: 'witnessed:s1#turn3'\nname: thing"

    def test_appends_when_the_key_is_absent(self) -> None:
        assert (
            set_frontmatter_scalar("uid: abc", "source_type", "witnessed")
            == "uid: abc\nsource_type: witnessed"
        )

    def test_never_touches_an_indented_key_of_the_same_name(self) -> None:
        fm = "field_sources:\n  source: api:apollo\nuid: abc"
        out = set_frontmatter_scalar(fm, "source", "witnessed")
        # The nested key is untouched; the top-level key was absent, so the
        # new one is appended.
        assert out == "field_sources:\n  source: api:apollo\nuid: abc\nsource: witnessed"


class TestYamlScalarLiteral:
    @pytest.mark.parametrize(
        "value,expected",
        [
            ("witnessed", "witnessed"),  # plain-safe channel label
            ("claude-opus-4-7", "claude-opus-4-7"),  # bare model id
            ("witnessed:s1#turn3", "'witnessed:s1#turn3'"),  # ':' and '#'
            ("s1#turn3", "'s1#turn3'"),
            ("abc:12", "'abc:12'"),
            ("", "''"),
            # A mid-string apostrophe needs no quoting in YAML; a LEADING
            # one does, and then every ``'`` in the value is doubled.
            ("it's", "it's"),
            ("'quoted'", "'''quoted'''"),
            (" leading-space", "' leading-space'"),
        ],
    )
    def test_quotes_exactly_what_yaml_would_misparse(
        self, value: str, expected: str
    ) -> None:
        assert yaml_scalar_literal(value) == expected


class TestWitnessedSourceFields:
    def test_field_mapping_and_write_order(self) -> None:
        fields = witnessed_source_fields(
            session_id="sess-1", origin_turn=3, witness_ref="led-a:17"
        )
        assert fields == {
            "source": "witnessed:sess-1#turn3",
            "source_type": "witnessed",
            "source_ref": "sess-1#turn3",
            "witness_ref": "led-a:17",
        }
        assert list(fields) == ["source", "source_type", "source_ref", "witness_ref"]

    def test_absent_turn_yields_the_bare_session_ref(self) -> None:
        fields = witnessed_source_fields(
            session_id="sess-1", origin_turn=None, witness_ref="led-a:17"
        )
        assert fields["source"] == "witnessed:sess-1"
        assert fields["source_ref"] == "sess-1"

    def test_witness_ref_key_is_omitted_not_blank(self) -> None:
        fields = witnessed_source_fields(
            session_id="sess-1", origin_turn=3, witness_ref=None
        )
        assert "witness_ref" not in fields

    def test_empty_session_id_raises_rather_than_writing_an_illegal_scalar(
        self,
    ) -> None:
        with pytest.raises(ValueError, match="non-empty session_id"):
            witnessed_source_fields(
                session_id="", origin_turn=3, witness_ref="led-a:17"
            )

    def test_turn_number_follows_origin_turn_not_the_ledger_counter(self) -> None:
        """Issue athenaeum#2159 AC 6, at the one site that composes the ref.

        The two numberings are deliberately DIFFERENT here: the memory's
        ``originTurn`` is 3, while the witness ledger's own per-session
        counter — which reaches the rewrite only as ``witness_ref``'s
        ``seq`` segment — is 41. ``#turn`` must follow the memory.
        """
        fields = witnessed_source_fields(
            session_id="sess-1", origin_turn=3, witness_ref="led-a:41"
        )
        assert fields["source"] == "witnessed:sess-1#turn3"
        assert fields["source_ref"] == "sess-1#turn3"
        assert "#turn41" not in fields["source"]
        assert fields["witness_ref"] == "led-a:41"

    def test_ref_matches_repairs_own_user_ref_numbering(self) -> None:
        # The ref builder is CALLED, not re-derived, so `user:<ref>` and
        # `witnessed:<ref>` are the same numbering by construction.
        from athenaeum.transcript_verify import _best_effort_ref

        ref = _best_effort_ref("sess-1", 3)
        fields = witnessed_source_fields(
            session_id="sess-1", origin_turn=3, witness_ref=None
        )
        assert fields["source"] == f"witnessed:{ref}"
        assert fields["source_ref"] == ref


class TestApplyFrontmatterScalars:
    def test_applies_every_field_with_the_right_quoting(self) -> None:
        fm = "uid: abc\nsource: claude:inferred\nname: thing"
        out = apply_frontmatter_scalars(
            fm,
            witnessed_source_fields(
                session_id="sess-1", origin_turn=3, witness_ref="led-a:41"
            ),
        )
        assert out == (
            "uid: abc\n"
            "source: 'witnessed:sess-1#turn3'\n"
            "name: thing\n"
            "source_type: witnessed\n"
            "source_ref: 'sess-1#turn3'\n"
            "witness_ref: 'led-a:41'"
        )

    def test_is_idempotent(self) -> None:
        fm = "uid: abc\nsource: claude:inferred"
        fields = witnessed_source_fields(
            session_id="sess-1", origin_turn=3, witness_ref="led-a:41"
        )
        once = apply_frontmatter_scalars(fm, fields)
        assert apply_frontmatter_scalars(once, fields) == once
