# SPDX-License-Identifier: Apache-2.0
"""Serve-time remote-profile pins (issue athenaeum#2383).

The acceptance criteria, one class each:

- :class:`TestDefaultPathIsUnchanged` (AC1, AC5) — with no new settings the
  served ``tools/list`` and a ``recall(with_pii=True)`` result are what they
  were BEFORE the pins existed. The "before" half is not a self-comparison:
  :data:`PRE_PIN_TOOL_INPUT_SCHEMAS` is the per-tool input-schema shape read
  off a server built from ``origin/develop`` at the merge base, checked in
  here, so a tool that gains a parameter fails this file rather than being
  caught by eye. It is the input-SCHEMA shape rather than a hash of the whole
  serialized payload deliberately: descriptions and FastMCP ``annotations``
  are allowed to change (athenaeum#2381 adds annotations to every
  registration), a tool's parameter set is not.
- :class:`TestRemoteProfileToolAllowlist` (AC2) — the expected name set is
  DERIVED (full live set minus the disabled names), never hand-listed, so a
  tool added to ``create_server`` tomorrow is covered with no edit here. The
  counter-example is its own test: an allowlist naming an unknown tool fails
  startup.
- :class:`TestExcludedFieldsPinOff` / :class:`TestExcludedFieldsPinOn` (AC3)
  — against a person page with excluded contact fields, asserting the LITERAL
  fixture values, not a flag.
- :class:`TestPinsResolveCliOverEnvOverYaml` (AC4) — per-pin precedence, and
  a non-zero ``athenaeum serve`` exit on an invalid value.

Unmarked: no network and no corpus scale beyond a two-page temp wiki.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from athenaeum import pii
from athenaeum.cli import main
from athenaeum.config import (
    resolve_serve_excluded_fields,
    resolve_serve_profile,
    resolve_serve_tools,
)
from athenaeum.remote_policy import (
    REMOTE_PROFILE,
    REMOTE_PROFILE_DISABLED_TOOLS,
    REMOTE_PROFILE_ENABLED_MUTATORS,
    RemotePolicyError,
    build_remote_policy,
)

#: `pii` routed off-corpus; the shape of the live `~/knowledge/athenaeum.yaml`
#: and of `tests/test_recall_with_pii.py`'s own fixture config.
EXCLUDED_CONFIG: dict[str, object] = {"storage": {"mapping": {"pii": "excluded"}}}

#: The fixture's excluded values. Asserted LITERALLY — a test that only
#: checked `with_pii`/`contact_included` would pass against a renderer that
#: leaked the address next to a flag saying it had not.
FIXTURE_EMAILS = ("alex@example.org", "a.widget@example.net")

#: Every pin's name, in every spelling a tool parameter could plausibly take
#: (AC5). None of these may appear as a property on any tool's input schema:
#: the pins are `create_server` arguments, so there is nothing a caller could
#: pass to widen its own surface.
PIN_PARAMETER_NAMES = frozenset(
    {
        "remote_policy",
        "profile",
        "serve_profile",
        "tools",
        "serve_tools",
        "allowed_tools",
        "excluded_fields",
        "serve_excluded_fields",
    }
)

#: Per-tool ``{name: {"properties": [...], "required": [...]}}`` read off a
#: server built from the MERGE BASE (`origin/develop`) before this issue's
#: change, and verified identical on this branch. This is AC1's "before" and
#: AC5's counter-example in one artifact: a tool that gains (or loses) a
#: parameter fails `test_tool_input_schemas_match_the_pre_pin_surface` naming
#: itself. Regenerate deliberately — from a checkout of the base ref, never by
#: pasting whatever this branch happens to produce.
PRE_PIN_TOOL_INPUT_SCHEMAS: dict[str, dict[str, list[str]]] = {
    "calibration_summary": {"properties": [], "required": []},
    "entity_schema": {"properties": [], "required": []},
    "enumerate_entities": {
        # Re-pinned for athenaeum#2538 AC7: `include_split` is the flag that
        # includes a split-created sub-page, excluded from this tool's
        # listing by default. Confirmed the ONLY delta against the prior
        # pin (no property removed, none reordered beyond its own
        # alphabetical slot) by diffing a live server's schema against this
        # map before editing it.
        "properties": [
            "cursor",
            "descending",
            "entity_type",
            "fields",
            "include_split",
            "limit",
            "predicates",
            "sort_key",
            "with_pii",
        ],
        "required": ["entity_type"],
    },
    "list_axiom_audit": {"properties": [], "required": []},
    "list_pending_decisions": {"properties": ["limit", "offset"], "required": []},
    "list_pending_merges": {"properties": ["full_body"], "required": []},
    "list_pending_questions": {"properties": ["limit", "offset"], "required": []},
    "raise_decision": {
        "properties": [
            "alternative",
            "context",
            "entity",
            "implemented_behavior",
            "issue_ref",
            "kind",
            "narrowed_scope",
            "question",
            "raiser",
            "repo",
        ],
        "required": [],
    },
    "read_entity": {
        "properties": ["entity_class", "include_excluded", "uid", "usage_classes"],
        "required": ["entity_class", "uid"],
    },
    "recall": {
        "properties": ["history", "query", "top_k", "type", "with_pii"],
        "required": ["query"],
    },
    "remember": {
        "properties": ["bucket", "content", "source", "sources", "valid_until"],
        "required": ["content"],
    },
    "resolve_merge": {"properties": ["decision", "id", "note"], "required": ["decision", "id"]},
    "resolve_question": {"properties": ["answer", "id"], "required": ["answer", "id"]},
    "review_audit_item": {
        "properties": ["human_verdict", "id", "note"],
        "required": ["human_verdict", "id"],
    },
    "scan_retraction_cascade": {"properties": [], "required": []},
}


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    """A knowledge root with one person page and its excluded record.

    Same shape as ``tests/test_recall_with_pii.py``'s fixture, with a SECOND
    excluded value so the handle-resolution test can assert on a value the
    caller did not itself supply as the query.
    """
    knowledge = tmp_path / "knowledge"
    wiki = knowledge / "wiki"
    wiki.mkdir(parents=True)
    (knowledge / "raw").mkdir(parents=True)
    (wiki / "alex.md").write_text(
        "---\nuid: alex\nname: Alex Widget\ntype: person\n---\n\n"
        "Alex Widget works on widget calibration.\n",
        encoding="utf-8",
    )
    records = pii.contacts_surface_root(knowledge, EXCLUDED_CONFIG)
    records.mkdir(parents=True, exist_ok=True)
    (records / "alex-contact.md").write_text(
        "---\nuid: alex\npii: true\nemails:\n"
        + "".join(f"  - {email}\n" for email in FIXTURE_EMAILS)
        + "---\n\nArchival data.\n",
        encoding="utf-8",
    )
    return knowledge


def _server(corpus_root: Path, **kwargs: object):
    pytest.importorskip("fastmcp")
    from athenaeum.mcp_server import create_server

    return create_server(
        raw_root=corpus_root / "raw",
        wiki_root=corpus_root / "wiki",
        cache_dir=corpus_root.parent / "cache",
        config=EXCLUDED_CONFIG,
        **kwargs,
    )


def _tools(server) -> list:
    async def _run() -> list:
        return sorted(await server.list_tools(), key=lambda t: t.name)

    return asyncio.run(_run())


def _tool_names(server) -> set[str]:
    return {tool.name for tool in _tools(server)}


def _input_schemas(server) -> dict[str, dict[str, list[str]]]:
    """``{tool: {"properties": [...], "required": [...]}}`` off a live server.

    Read through FastMCP's own ``list_tools()`` — the introspection API
    ``tests/test_security_posture_tool_parity.py`` and
    ``tests/test_recall_tool_schema_parity.py`` already use — never from the
    Python signatures, so what is compared is what a client is actually served.
    """
    out: dict[str, dict[str, list[str]]] = {}
    for tool in _tools(server):
        schema = tool.parameters or {}
        out[tool.name] = {
            "properties": sorted((schema.get("properties") or {}).keys()),
            "required": sorted(schema.get("required") or []),
        }
    return out


def _served_shape(server) -> str:
    """A stable serialization of what ``tools/list`` serves, for an equality
    comparison between two servers built in the SAME process (so FastMCP's
    version, this corpus and these descriptions are all held constant)."""
    return json.dumps(
        [
            {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
            }
            for tool in _tools(server)
        ],
        indent=2,
        sort_keys=True,
        default=str,
    )


def _call(server, name: str, **kwargs: object):
    async def _run():
        tool = await server.get_tool(name)
        return tool.fn

    return _run and asyncio.run(_run())(**kwargs)


class TestDefaultPathIsUnchanged:
    """AC1 and AC5: no new settings, nothing changes; and no tool gained a
    parameter under ANY setting."""

    def test_tool_input_schemas_match_the_pre_pin_surface(self, corpus: Path) -> None:
        live = _input_schemas(_server(corpus))
        # Sanity: neither side degenerated to empty, the failure mode an
        # equality assertion alone cannot catch.
        assert live, "sanity: create_server registered zero tools"
        assert PRE_PIN_TOOL_INPUT_SCHEMAS, "sanity: the checked-in base map is empty"
        assert live == PRE_PIN_TOOL_INPUT_SCHEMAS

    def test_omitting_the_pin_argument_equals_passing_none(self, corpus: Path) -> None:
        """`remote_policy=None` is not a third behaviour — it IS the default."""
        assert _served_shape(_server(corpus)) == _served_shape(
            _server(corpus, remote_policy=None)
        )

    def test_recall_with_pii_still_returns_the_excluded_values(self, corpus: Path) -> None:
        rendered = _call(_server(corpus), "recall", query="widget", with_pii=True)
        for email in FIXTURE_EMAILS:
            assert email in rendered

    def test_recall_with_pii_output_is_identical_with_the_argument_passed(
        self, corpus: Path
    ) -> None:
        """AC1's "every tool's output" half: byte-identical, not merely
        'still contains the values'."""
        without = _call(_server(corpus), "recall", query="widget", with_pii=True)
        explicit_none = _call(
            _server(corpus, remote_policy=None), "recall", query="widget", with_pii=True
        )
        assert without == explicit_none
        # Not vacuous: both outputs really do carry the values, so this is an
        # equality between two populated joins, not between two empty ones.
        assert FIXTURE_EMAILS[0] in without

    def test_read_entity_still_returns_the_excluded_values(self, corpus: Path) -> None:
        payload = json.loads(
            _call(
                _server(corpus),
                "read_entity",
                uid="alex",
                entity_class="person",
                include_excluded=True,
            )
        )
        assert "excluded_fields_withheld" not in payload
        assert FIXTURE_EMAILS[0] in payload["contact"]["emails"]

    @pytest.mark.parametrize(
        "policy_kwargs",
        [
            {},
            {"remote_policy": None},
            {"remote_policy": build_remote_policy(profile=REMOTE_PROFILE)},
            {"remote_policy": build_remote_policy(excluded_fields=False)},
        ],
        ids=["omitted", "none", "remote-profile", "excluded-fields-off"],
    )
    def test_no_tool_input_schema_carries_a_pin_parameter(
        self, corpus: Path, policy_kwargs: dict
    ) -> None:
        """AC5: the pins live in `create_server` ARGUMENTS. Checked under the
        pins as well as without them — a pin that leaked into a schema only
        when active would be just as forgeable."""
        schemas = _input_schemas(_server(corpus, **policy_kwargs))
        assert schemas, "sanity: zero tools served"
        for name, schema in schemas.items():
            leaked = PIN_PARAMETER_NAMES.intersection(schema["properties"])
            assert not leaked, f"{name} gained pin parameter(s) {sorted(leaked)}"


class TestRemoteProfileToolAllowlist:
    """AC2: the allowlist gates REGISTRATION, so a disabled tool is absent."""

    def test_remote_profile_omits_exactly_the_three_owner_only_mutators(
        self, corpus: Path
    ) -> None:
        full = _tool_names(_server(corpus))
        # The subtraction must be real: if a disabled name were not in the
        # full set, the derived expectation would equal the full set and this
        # test would pass while proving nothing.
        assert REMOTE_PROFILE_DISABLED_TOOLS <= full
        expected = full - REMOTE_PROFILE_DISABLED_TOOLS
        served = _tool_names(
            _server(corpus, remote_policy=build_remote_policy(profile=REMOTE_PROFILE))
        )
        assert served == expected
        # D5: the two append-only mutators stay available.
        assert REMOTE_PROFILE_ENABLED_MUTATORS <= served

    def test_an_explicit_allowlist_serves_exactly_the_named_tools(self, corpus: Path) -> None:
        allowed = frozenset({"recall", "read_entity"})
        served = _tool_names(
            _server(corpus, remote_policy=build_remote_policy(tools=allowed))
        )
        assert served == allowed

    def test_the_allowlist_and_the_profile_compose(self, corpus: Path) -> None:
        """A tool must clear BOTH: an allowlist cannot re-enable what the
        profile disabled."""
        served = _tool_names(
            _server(
                corpus,
                remote_policy=build_remote_policy(
                    profile=REMOTE_PROFILE,
                    tools=frozenset({"recall", "resolve_question"}),
                ),
            )
        )
        assert served == {"recall"}

    def test_an_allowlist_naming_an_unknown_tool_fails_startup(self, corpus: Path) -> None:
        """AC2's counter-example. The message names the unknown entry AND what
        this deployment does serve — a bare "invalid" would leave the operator
        guessing which of several names they typo'd."""
        with pytest.raises(RemotePolicyError) as excinfo:
            _server(
                corpus,
                remote_policy=build_remote_policy(tools=frozenset({"recall", "recal"})),
            )
        message = str(excinfo.value)
        assert "recal" in message
        assert "read_entity" in message

    def test_a_disabled_tool_is_absent_rather_than_refusing(self, corpus: Path) -> None:
        """The distinction the AC draws: a disabled tool is not registered, so
        CALLING it is a "no such tool" error from FastMCP's own dispatch --
        never a refusal payload from a tool body that ran. Asserted through
        `call_tool`, not just the `tools/list` names, because a client that
        hardcoded the name is exactly the caller this has to stop."""
        server = _server(corpus, remote_policy=build_remote_policy(profile=REMOTE_PROFILE))
        with pytest.raises(Exception) as excinfo:
            asyncio.run(server.call_tool("resolve_question", {"id": "q1", "answer": "x"}))
        assert "resolve_question" in str(excinfo.value)
        # Positive control: a tool the profile LEAVES ON dispatches fine, so
        # the raise above is the allowlist and not a broken call shape.
        assert asyncio.run(server.call_tool("entity_schema", {})) is not None


class TestExcludedFieldsPinOff:
    """AC3 with `excluded_fields: off`."""

    @pytest.fixture
    def server(self, corpus: Path):
        return _server(corpus, remote_policy=build_remote_policy(profile=REMOTE_PROFILE))

    def test_recall_with_pii_returns_none_of_the_excluded_values(self, server) -> None:
        rendered = _call(server, "recall", query="widget", with_pii=True)
        # The hit itself is still returned -- this withholds values, not pages.
        assert "Alex Widget" in rendered
        for email in FIXTURE_EMAILS:
            assert email not in rendered

    def test_recall_says_the_values_were_withheld(self, server) -> None:
        from athenaeum.mcp_server import EXCLUDED_FIELDS_WITHHELD_NOTICE

        rendered = _call(server, "recall", query="widget", with_pii=True)
        assert EXCLUDED_FIELDS_WITHHELD_NOTICE in rendered

    def test_the_withheld_notice_discloses_no_value_and_no_length(self) -> None:
        """The notice is handed to the caller the pin exists to keep values
        away from, so it must not itself be a channel."""
        from athenaeum.mcp_server import EXCLUDED_FIELDS_WITHHELD_NOTICE

        for email in FIXTURE_EMAILS:
            assert email not in EXCLUDED_FIELDS_WITHHELD_NOTICE
            assert email.split("@")[1] not in EXCLUDED_FIELDS_WITHHELD_NOTICE
        assert not any(ch.isdigit() for ch in EXCLUDED_FIELDS_WITHHELD_NOTICE)

    def test_a_handle_shaped_query_returns_none_of_the_excluded_values(self, server) -> None:
        """The reverse-lookup route, which a gate inside
        `_excluded_block_for_hit` alone would have left wide open: a
        handle-shaped query resolves through `identity_resolution` BEFORE any
        ranking. Asserted on the SECOND fixture value, which the caller did
        not supply as the query, so an echo of the query cannot pass this."""
        rendered = _call(server, "recall", query=FIXTURE_EMAILS[0], with_pii=True)
        assert FIXTURE_EMAILS[1] not in rendered

    def test_a_handle_shaped_query_still_returns_parseable_json(self, server) -> None:
        """Regression (Seer review on PR athenaeum#2416): the handle path
        returns a strict JSON document, so the notice must go INSIDE the
        object. Appending a markdown line left trailing prose after the
        closing brace and broke every client that parses that payload."""
        from athenaeum.mcp_server import EXCLUDED_FIELDS_WITHHELD_REASON

        rendered = _call(server, "recall", query=FIXTURE_EMAILS[0], with_pii=True)
        payload = json.loads(rendered)  # raises on trailing prose
        assert payload["excluded_fields_withheld"] == EXCLUDED_FIELDS_WITHHELD_REASON
        # The handle payload's own keys survive the round-trip.
        assert payload["uid"] == "alex"
        assert payload["contact_values"] == []

    def test_the_similarity_path_still_gets_the_markdown_notice(self, server) -> None:
        """The other half of the same fix: markdown output is NOT turned into
        JSON, so the two media stay distinguishable."""
        from athenaeum.mcp_server import EXCLUDED_FIELDS_WITHHELD_NOTICE

        rendered = _call(server, "recall", query="widget", with_pii=True)
        assert rendered.endswith(EXCLUDED_FIELDS_WITHHELD_NOTICE)

    def test_read_entity_returns_none_of_the_excluded_values(self, server) -> None:
        raw = _call(
            server,
            "read_entity",
            uid="alex",
            entity_class="person",
            include_excluded=True,
        )
        for email in FIXTURE_EMAILS:
            assert email not in raw
        payload = json.loads(raw)
        assert payload["contact"] == {}
        assert payload["contact_included"] is False

    def test_read_entity_reports_the_pin_rather_than_looking_like_no_record(
        self, server
    ) -> None:
        from athenaeum.mcp_server import EXCLUDED_FIELDS_WITHHELD_REASON

        payload = json.loads(
            _call(
                server,
                "read_entity",
                uid="alex",
                entity_class="person",
                include_excluded=True,
            )
        )
        assert payload["excluded_fields_withheld"] == EXCLUDED_FIELDS_WITHHELD_REASON

    def test_enumerate_entities_refuses_a_pii_scoped_predicate(self, server) -> None:
        result = _call(
            server,
            "enumerate_entities",
            entity_type="person",
            predicates=[
                {"fields": "google_contact_primary", "kind": "eq", "value": "x"}
            ],
            with_pii=True,
        )
        assert result["hits"] == []
        assert "google_contact_primary" in (result["error"] or "")

    def test_enumerate_entities_refuses_a_pii_scoped_output_field(self, server) -> None:
        result = _call(
            server,
            "enumerate_entities",
            entity_type="person",
            fields=["google_contact_primary"],
            with_pii=True,
        )
        assert result["hits"] == []
        assert "google_contact_primary" in (result["error"] or "")

    def test_enumerate_entities_still_answers_a_non_pii_query(self, server) -> None:
        """The pin refuses PII-scoped fields, not the tool."""
        result = _call(server, "enumerate_entities", entity_type="person")
        assert result.get("error") is None
        assert [hit["uid"] for hit in result["hits"]] == ["alex"]


class TestExcludedFieldsPinOn:
    """AC3's second half: with `on`, all three behave as today — compared
    against the no-policy server rather than re-asserted by hand."""

    @pytest.fixture
    def server(self, corpus: Path):
        return _server(
            corpus,
            remote_policy=build_remote_policy(
                profile=REMOTE_PROFILE, excluded_fields=True
            ),
        )

    def test_recall_with_pii_is_identical_to_the_unpinned_server(
        self, corpus: Path, server
    ) -> None:
        pinned = _call(server, "recall", query="widget", with_pii=True)
        unpinned = _call(_server(corpus), "recall", query="widget", with_pii=True)
        assert pinned == unpinned
        assert FIXTURE_EMAILS[0] in pinned

    def test_read_entity_is_identical_to_the_unpinned_server(
        self, corpus: Path, server
    ) -> None:
        kwargs = {"uid": "alex", "entity_class": "person", "include_excluded": True}
        pinned = _call(server, "read_entity", **kwargs)
        unpinned = _call(_server(corpus), "read_entity", **kwargs)
        assert pinned == unpinned
        assert FIXTURE_EMAILS[0] in pinned

    def test_enumerate_entities_does_not_refuse_a_pii_scoped_field(self, server) -> None:
        result = _call(
            server,
            "enumerate_entities",
            entity_type="person",
            fields=["google_contact_primary"],
            with_pii=True,
        )
        assert result.get("error") is None


class TestPinsResolveCliOverEnvOverYaml:
    """AC4: CLI > env > yaml per pin, and a loud non-zero exit on a bad value."""

    @pytest.fixture(autouse=True)
    def _clear_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for name in (
            "ATHENAEUM_SERVE_PROFILE",
            "ATHENAEUM_SERVE_TOOLS",
            "ATHENAEUM_SERVE_EXCLUDED_FIELDS",
        ):
            monkeypatch.delenv(name, raising=False)

    def test_profile_reads_yaml(self) -> None:
        assert resolve_serve_profile({"serve": {"profile": "remote"}}) == REMOTE_PROFILE

    def test_profile_env_beats_yaml(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATHENAEUM_SERVE_PROFILE", "remote")
        assert resolve_serve_profile({"serve": {"profile": "nonsense"}}) == REMOTE_PROFILE

    def test_profile_cli_beats_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATHENAEUM_SERVE_PROFILE", "nonsense")
        assert (
            resolve_serve_profile({"serve": {"profile": "nonsense"}}, "remote")
            == REMOTE_PROFILE
        )

    def test_profile_unset_is_none(self) -> None:
        assert resolve_serve_profile(None) is None
        assert resolve_serve_profile({}) is None

    @pytest.mark.parametrize("tier", ["yaml", "env", "cli"])
    def test_an_invalid_profile_raises_at_every_tier(
        self, tier: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config: dict[str, object] = {}
        cli = None
        if tier == "yaml":
            config = {"serve": {"profile": "local"}}
        elif tier == "env":
            monkeypatch.setenv("ATHENAEUM_SERVE_PROFILE", "local")
        else:
            cli = "local"
        with pytest.raises(RemotePolicyError, match="local"):
            resolve_serve_profile(config, cli)

    def test_tools_reads_yaml_as_a_list_or_a_comma_string(self) -> None:
        assert resolve_serve_tools({"serve": {"tools": ["recall", "remember"]}}) == frozenset(
            {"recall", "remember"}
        )
        assert resolve_serve_tools({"serve": {"tools": "recall, remember"}}) == frozenset(
            {"recall", "remember"}
        )

    def test_tools_env_beats_yaml(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATHENAEUM_SERVE_TOOLS", "recall")
        assert resolve_serve_tools({"serve": {"tools": ["remember"]}}) == frozenset({"recall"})

    def test_tools_cli_beats_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATHENAEUM_SERVE_TOOLS", "remember")
        assert resolve_serve_tools({"serve": {"tools": ["remember"]}}, "recall") == frozenset(
            {"recall"}
        )

    def test_tools_unset_or_empty_is_no_allowlist(self) -> None:
        assert resolve_serve_tools(None) is None
        assert resolve_serve_tools({"serve": {"tools": []}}) is None
        assert resolve_serve_tools({}, "") is None

    def test_excluded_fields_reads_yaml(self) -> None:
        assert resolve_serve_excluded_fields({"serve": {"excluded_fields": "off"}}) is False
        assert resolve_serve_excluded_fields({"serve": {"excluded_fields": "on"}}) is True

    def test_excluded_fields_env_beats_yaml(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATHENAEUM_SERVE_EXCLUDED_FIELDS", "off")
        assert resolve_serve_excluded_fields({"serve": {"excluded_fields": "on"}}) is False

    def test_excluded_fields_cli_beats_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATHENAEUM_SERVE_EXCLUDED_FIELDS", "on")
        assert (
            resolve_serve_excluded_fields({"serve": {"excluded_fields": "on"}}, "off") is False
        )

    def test_excluded_fields_unset_is_none(self) -> None:
        assert resolve_serve_excluded_fields(None) is None
        assert resolve_serve_excluded_fields({}) is None

    def test_an_invalid_excluded_fields_value_raises(self) -> None:
        with pytest.raises(RemotePolicyError, match="maybe"):
            resolve_serve_excluded_fields({"serve": {"excluded_fields": "maybe"}})

    @pytest.mark.parametrize("value", [True, 7, ["remote"], {"name": "remote"}])
    def test_a_non_string_profile_value_raises_rather_than_falling_open(
        self, value: object
    ) -> None:
        """Regression (Seer review on PR athenaeum#2416): a non-string
        `serve.profile` used to fall through to "no profile", which serves the
        FULL surface -- the one direction a safety pin must never fail in."""
        with pytest.raises(RemotePolicyError, match="serve.profile"):
            resolve_serve_profile({"serve": {"profile": value}})

    @pytest.mark.parametrize("value", [True, 7, {"name": "recall"}])
    def test_a_non_list_non_string_tools_value_raises(self, value: object) -> None:
        """Same fail-open class on the allowlist: a scalar or mapping would
        otherwise resolve to "no allowlist" and serve every tool."""
        with pytest.raises(RemotePolicyError, match="serve.tools"):
            resolve_serve_tools({"serve": {"tools": value}})

    def test_an_empty_tools_list_is_still_unset_not_an_error(self) -> None:
        """The documented convention, kept distinct from the typo cases above:
        to narrow the surface you must name at least one tool."""
        assert resolve_serve_tools({"serve": {"tools": []}}) is None

    def test_a_yaml_boolean_excluded_fields_value_raises(self) -> None:
        """`serve.excluded_fields: false` is YAML's bool, not this knob's
        vocabulary. Guessing which of off/on it meant would be a guess on a
        PII-egress boundary."""
        with pytest.raises(RemotePolicyError, match="boolean"):
            resolve_serve_excluded_fields({"serve": {"excluded_fields": False}})

    def test_the_profile_supplies_the_excluded_fields_default(self) -> None:
        assert build_remote_policy(profile=REMOTE_PROFILE).excluded_fields is False
        assert (
            build_remote_policy(profile=REMOTE_PROFILE, excluded_fields=True).excluded_fields
            is True
        )

    def test_no_pin_set_at_all_builds_no_policy(self) -> None:
        assert build_remote_policy() is None


class TestServeExitsNonZeroOnAnInvalidPin:
    """AC4's startup half, through the real CLI — `resolve_screening`'s shape:
    print the reason and return a non-zero code rather than serving."""

    @pytest.fixture
    def knowledge(self, tmp_path: Path) -> Path:
        root = tmp_path / "knowledge"
        (root / "wiki").mkdir(parents=True)
        (root / "raw").mkdir(parents=True)
        return root

    @pytest.fixture(autouse=True)
    def _no_real_server(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Never actually run a server: assert on the exit code, and make a
        server that DID get built fail loudly rather than block."""
        import athenaeum.mcp_server as mcp_mod

        real = mcp_mod.create_server

        class _FakeServer:
            def run(self) -> None:
                raise KeyboardInterrupt

        def _fake(**kwargs: object) -> object:
            # Still exercise the real construction (that is where the
            # allowlist is validated), then hand back a server that exits.
            real(**kwargs)
            return _FakeServer()

        monkeypatch.setattr(mcp_mod, "create_server", _fake)

    def test_an_invalid_profile_in_yaml_exits_non_zero(
        self, knowledge: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        (knowledge / "athenaeum.yaml").write_text("serve:\n  profile: local\n")
        assert main(["serve", "--path", str(knowledge)]) == 1
        assert "local" in capsys.readouterr().err

    def test_an_invalid_excluded_fields_value_exits_non_zero(
        self, knowledge: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        (knowledge / "athenaeum.yaml").write_text("serve:\n  excluded_fields: maybe\n")
        assert main(["serve", "--path", str(knowledge)]) == 1
        assert "maybe" in capsys.readouterr().err

    def test_an_allowlist_naming_an_unknown_tool_exits_non_zero(
        self, knowledge: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        pytest.importorskip("fastmcp")
        (knowledge / "athenaeum.yaml").write_text("serve:\n  tools:\n    - recal\n")
        assert main(["serve", "--path", str(knowledge)]) == 1
        assert "recal" in capsys.readouterr().err

    def test_a_valid_profile_starts_the_server(self, knowledge: Path) -> None:
        """The positive control: the same path with a VALID value reaches
        `server.run()`, so a non-zero exit above is the pin and not an
        unrelated startup failure."""
        pytest.importorskip("fastmcp")
        (knowledge / "athenaeum.yaml").write_text("serve:\n  profile: remote\n")
        assert main(["serve", "--path", str(knowledge)]) == 0
