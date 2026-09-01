# SPDX-License-Identifier: Apache-2.0
"""The MCP and CLI generic read surfaces (issue athenaeum#886).

athenaeum#883 made the primitives class-generic and athenaeum#885 wired `recall`;
after those, a caller reaching athenaeum over MCP or the shell could still only
ask the person-shaped question. These tests pin the caller-facing surfaces:

- ``TestMcpRecallToolFlag`` — the `@mcp.tool() recall` tool takes `with_pii`
  and threads it; default behaviour for every existing caller is unchanged.
- ``TestMcpReadEntityTool`` — the generic tool carries ALL of `uid`, the
  entity class, `include_excluded` AND `usage_classes` (dropping the last is
  not a smaller version of the same tool — it removes a filter
  `docs/security-posture.md` §2.3 depends on), and its fail-closed audience
  check.
- ``TestCliEntityCommand`` — the same for the shell: `athenaeum query entity`
  prints the same JSON object shape.
- ``TestCliRecallWithPii`` — `athenaeum recall --with-pii`. `cmd_recall` is a
  SECOND implementation of the layer ordering (it builds its own backend and
  its own authorization/recallable drops), so its layer ordering is asserted
  HERE against that code path — athenaeum#885's tests do not cover it.

The person-shaped `read_person` MCP tool and `athenaeum query person` CLI
command this module once pinned parity against were removed in athenaeum#888;
their coverage is `entity_read`/`cmd_entity`'s alone now.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from athenaeum import pii
from athenaeum._cli_shared import EXIT_INTERNAL_ERROR, EXIT_NOT_FOUND
from athenaeum._cmd_query import cmd_entity, cmd_recall
from athenaeum.mcp_server import entity_read

EXCLUDED_CONFIG: dict[str, object] = {"storage": {"mapping": {"pii": "excluded"}}}

RESTRICTED = {"secondary"}


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


def _write_record(root: Path, filename: str, *, uid: str, fields: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / filename
    path.write_text(
        f"---\nuid: {uid}\npii: true\n{fields}---\n\nArchival data.\n",
        encoding="utf-8",
    )
    return path


def _write_config(knowledge: Path) -> None:
    """Write the athenaeum.yaml the CLI commands load for themselves."""
    knowledge.mkdir(parents=True, exist_ok=True)
    (knowledge / "athenaeum.yaml").write_text(
        "storage:\n  mapping:\n    pii: excluded\n", encoding="utf-8"
    )


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    knowledge = tmp_path / "knowledge"
    _write_config(knowledge)
    _write_page(knowledge / "wiki", "alex", name="Alex Widget")
    _write_record(
        pii.contacts_surface_root(knowledge, EXCLUDED_CONFIG),
        "alex-contact.md",
        uid="alex",
        fields="emails:\n  - alex@example.org\n",
    )
    return knowledge


def _tool(server, name: str):
    import asyncio

    async def _run():
        tools = await server.list_tools()
        return next(t for t in tools if t.name == name)

    return asyncio.run(_run())


def _server(knowledge: Path, *, caller_audience: set[str] | None = None):
    pytest.importorskip("fastmcp")
    from athenaeum.mcp_server import create_server

    raw = knowledge / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    return create_server(
        raw_root=raw,
        wiki_root=knowledge / "wiki",
        caller_audience=caller_audience,
        config=EXCLUDED_CONFIG,
    )


class TestMcpRecallToolFlag:
    def test_recall_tool_declares_with_pii(self, corpus: Path) -> None:
        tool = _tool(_server(corpus), "recall")

        assert "with_pii" in tool.parameters["properties"]
        assert tool.parameters["properties"]["with_pii"]["default"] is False

    def test_recall_tool_default_is_unchanged(self, corpus: Path) -> None:
        tool = _tool(_server(corpus), "recall")

        result = tool.fn("widget")

        assert "Alex Widget" in result
        assert "alex@example.org" not in result

    def test_recall_tool_threads_the_flag(self, corpus: Path) -> None:
        tool = _tool(_server(corpus), "recall")

        result = tool.fn("widget", with_pii=True)

        assert "alex@example.org" in result


class TestMcpReadEntityTool:
    def test_tool_carries_all_four_arguments(self, corpus: Path) -> None:
        """Dropping `usage_classes` would silently remove a §2.3 filter."""
        tool = _tool(_server(corpus), "read_entity")

        props = tool.parameters["properties"]
        assert set(props) >= {"uid", "entity_class", "include_excluded", "usage_classes"}
        assert props["include_excluded"]["default"] is False

    def test_reads_a_person_through_the_generic_tool(self, corpus: Path) -> None:
        tool = _tool(_server(corpus), "read_entity")

        payload = json.loads(tool.fn("alex", "person", True))

        assert payload["uid"] == "alex"
        assert payload["contact"] == {"emails": ["alex@example.org"]}

    def test_usage_classes_filter_is_honoured(self, corpus: Path) -> None:
        tool = _tool(_server(corpus), "read_entity")

        payload = json.loads(tool.fn("alex", "person", True, ["observed"]))

        # The record carries no classification, so its value is `unclassified`.
        assert payload["contact"] == {}

    def test_unknown_uid_is_a_json_not_found(self, corpus: Path) -> None:
        tool = _tool(_server(corpus), "read_entity")

        payload = json.loads(tool.fn("nobody", "person"))

        assert payload["ok"] is False
        assert "not found" in payload["error"]

    def test_reads_a_non_person_class(self, tmp_path: Path) -> None:
        knowledge = tmp_path / "knowledge"
        config: dict[str, object] = {
            "storage": {
                "mapping": {"vendor": "vendor-excluded"},
                "adapters": {
                    "vendor-excluded": {
                        "backing_store": "markdown",
                        "surface_root": "vendor-excluded",
                        "corpus_policy": {
                            "embedded": False,
                            "recallable": False,
                            "merge_eligible": False,
                        },
                    }
                },
            }
        }
        _write_page(knowledge / "wiki", "acme", name="Acme Ltd", entity_type="vendor")
        _write_record(
            pii.excluded_surface_root("vendor", knowledge, config),
            "acme.md",
            uid="acme",
            fields="account_numbers:\n  - A-1\n",
        )

        payload = json.loads(
            entity_read(
                knowledge,
                "acme",
                page_class="vendor",
                include_excluded=True,
                config=config,
            )
        )

        assert payload["contact"] == {"account_numbers": ["A-1"]}

    def test_fail_closed_audience_check_applies_on_the_generic_path(
        self, corpus: Path
    ) -> None:
        """A restricted caller never receives a value from the tool."""
        generic = entity_read(
            corpus,
            "alex",
            page_class="person",
            include_excluded=True,
            caller_audience=RESTRICTED,
            config=EXCLUDED_CONFIG,
        )

        assert "alex@example.org" not in generic

    def test_authorized_restricted_caller_receives_the_value(
        self, tmp_path: Path
    ) -> None:
        knowledge = tmp_path / "knowledge"
        _write_config(knowledge)
        _write_page(
            knowledge / "wiki", "alex", name="Alex Widget", extra="access: open\n"
        )
        _write_record(
            pii.contacts_surface_root(knowledge, EXCLUDED_CONFIG),
            "alex-contact.md",
            uid="alex",
            fields="emails:\n  - alex@example.org\n",
        )

        payload = json.loads(
            entity_read(
                knowledge,
                "alex",
                page_class="person",
                include_excluded=True,
                caller_audience=RESTRICTED,
                config=EXCLUDED_CONFIG,
            )
        )

        assert payload["contact"] == {"emails": ["alex@example.org"]}


class TestDateTypedFrontmatterCoercion:
    """A bare YAML date/datetime must not crash JSON serialization (athenaeum#1002).

    ``yaml.safe_load`` (``parse_frontmatter``) parses an unquoted frontmatter
    date into ``datetime.date`` and a timestamp into ``datetime.datetime`` —
    neither serializable by plain ``json.dumps``. Before the fix, a page like
    the reported ``9660b25b`` crashed every read tool with ``Object of type
    date is not JSON serializable``. These pin the fix at the ONE shared
    coercion point (:func:`athenaeum.pii.json_date_default`), exercised
    across ``read_entity`` and both of ``recall``'s
    ``with_pii=True`` JSON-emitting branches (the ordinary similarity-search
    facts block AND the handle-shaped exact-lookup document) — so a fix
    landed on only one of these would leave this failing on the others.
    """

    def test_read_entity_round_trips_a_bare_date_and_a_datetime(
        self, tmp_path: Path
    ) -> None:
        knowledge = tmp_path / "knowledge"
        _write_config(knowledge)
        _write_page(
            knowledge / "wiki",
            "alex",
            name="Alex Widget",
            extra="dob: 1990-01-01\nlast_synced: 2026-03-01T10:30:00\n",
        )

        payload = json.loads(
            entity_read(knowledge, "alex", page_class="person", config=EXCLUDED_CONFIG)
        )

        assert payload["frontmatter"]["dob"] == "1990-01-01"
        assert payload["frontmatter"]["last_synced"] == "2026-03-01T10:30:00"

    def test_read_entity_round_trips_with_excluded_data_included_too(
        self, corpus: Path
    ) -> None:
        """The crash reproduces regardless of ``include_excluded`` — the
        raw ``frontmatter`` field is embedded either way."""
        page = corpus / "wiki" / "alex.md"
        page.write_text(
            page.read_text(encoding="utf-8").replace(
                "type: person\n", "type: person\ndob: 1990-01-01\n"
            ),
            encoding="utf-8",
        )

        payload = json.loads(
            entity_read(
                corpus,
                "alex",
                page_class="person",
                include_excluded=True,
                config=EXCLUDED_CONFIG,
            )
        )

        assert payload["frontmatter"]["dob"] == "1990-01-01"
        assert payload["contact"] == {"emails": ["alex@example.org"]}

    def test_mcp_read_entity_tool_round_trips_a_dated_page(self, tmp_path: Path) -> None:
        knowledge = tmp_path / "knowledge"
        _write_config(knowledge)
        _write_page(
            knowledge / "wiki", "alex", name="Alex Widget", extra="dob: 1990-01-01\n"
        )
        tool = _tool(_server(knowledge), "read_entity")

        payload = json.loads(tool.fn("alex", "person"))

        assert payload["frontmatter"]["dob"] == "1990-01-01"

    def test_cli_entity_command_round_trips_a_dated_page(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """CLI mirror of ``test_read_entity_round_trips_a_bare_date_and_a_datetime``
        (issue athenaeum#1110) — ``athenaeum entity``'s own
        ``_read_entity_to_stdout``'s ``json.dumps(result.to_dict(), indent=2)``
        call had no ``default=`` at all before this fix. This is the exact
        ``TypeError: Object of type date is not JSON serializable`` crash
        athenaeum#1110 reports, on the shell entry point rather than the MCP
        tool or the primitive `_cmd_query.py`.

        Covers both a bare date AND a datetime, and pins the ISO-8601 form
        (not ``str()``'s space-separated datetime rendering) exactly as the
        MCP-side test above pins it for ``read_entity``.
        """
        knowledge = tmp_path / "knowledge"
        _write_config(knowledge)
        _write_page(
            knowledge / "wiki",
            "alex",
            name="Alex Widget",
            extra="dob: 1990-01-01\nlast_synced: 2026-03-01T10:30:00\n",
        )

        rc = cmd_entity(_entity_args(knowledge, "alex"))
        payload = json.loads(capsys.readouterr().out)

        assert rc == 0
        assert payload["frontmatter"]["dob"] == "1990-01-01"
        assert payload["frontmatter"]["last_synced"] == "2026-03-01T10:30:00"

    def _dated_source_corpus(self, tmp_path: Path) -> Path:
        """A person with an excluded value whose classification ``source`` is
        a BARE (unquoted) YAML datetime — the vector for ``recall``'s crash,
        distinct from the page-frontmatter vector above: neither JSON-emitting
        branch of ``recall`` ever embeds the page's own raw frontmatter, only
        ``ContactClassification.source``/``ContactValueFact.source``, which
        read straight off the excluded RECORD's frontmatter uncoerced."""
        knowledge = tmp_path / "knowledge"
        _write_page(knowledge / "wiki", "alex", name="Alex Widget")
        _write_record(
            pii.contacts_surface_root(knowledge, EXCLUDED_CONFIG),
            "alex-contact.md",
            uid="alex",
            fields=(
                "emails:\n"
                "  - alex@example.org\n"
                "contact_classification:\n"
                "  - identifier: alex@example.org\n"
                "    usage_class: observed\n"
                "    source: 2026-03-01T10:30:00\n"
            ),
        )
        return knowledge

    def test_recall_with_pii_similarity_search_coerces_the_source_datetime(
        self, tmp_path: Path
    ) -> None:
        """Also pins the ISO-8601 format itself: the prior ``default=str``
        fallback rendered a datetime as ``'2026-03-01 10:30:00'`` (space
        separator, ``str()``'s default) — not ISO-8601. This asserts the
        actual ``'T'``-separated form, so a regression back to ``default=str``
        would fail even though it no longer crashes."""
        knowledge = self._dated_source_corpus(tmp_path)
        tool = _tool(_server(knowledge), "recall")

        result = tool.fn("widget", with_pii=True)

        assert '"source": "2026-03-01T10:30:00"' in result
        assert "2026-03-01 10:30:00" not in result

    def test_recall_with_pii_handle_shaped_query_does_not_crash(
        self, tmp_path: Path
    ) -> None:
        """The handle-shaped path (``recall("alex@example.org", with_pii=True)``)
        had NO ``default=`` fallback at all before the fix — a raw datetime
        ``source`` crashed it outright, not merely mis-formatted it."""
        knowledge = self._dated_source_corpus(tmp_path)
        tool = _tool(_server(knowledge), "recall")

        result = tool.fn("alex@example.org", with_pii=True)
        payload = json.loads(result)

        assert payload["resolved"] is True
        assert payload["contact_values"][0]["source"] == "2026-03-01T10:30:00"

    def test_cli_recall_handle_shaped_query_coerces_the_source_datetime(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """CLI mirror of ``test_recall_with_pii_handle_shaped_query_does_not_crash``
        (issue athenaeum#1110) — ``athenaeum recall``'s own
        ``print(json.dumps(handle_resolution.to_dict(), indent=2,
        sort_keys=True))`` call had NO ``default=`` fallback at all before this
        fix, identical to the pre-athenaeum#1002 gap in the MCP tool's mirror of
        this exact call. Also pins the ISO-8601 ``'T'``-separated form."""
        knowledge = self._dated_source_corpus(tmp_path)
        # Unlike the MCP tool above (which is handed `config=EXCLUDED_CONFIG`
        # directly), `cmd_recall` loads `athenaeum.yaml` from disk itself —
        # write the same mapping so it resolves the same contacts surface
        # `_dated_source_corpus` populated.
        _write_config(knowledge)

        rc = cmd_recall(_recall_args(knowledge, "alex@example.org", with_pii=True))
        payload = json.loads(capsys.readouterr().out)

        assert rc == 0
        assert payload["resolved"] is True
        assert payload["contact_values"][0]["source"] == "2026-03-01T10:30:00"


def _entity_args(knowledge: Path, uid: str, **kwargs: object) -> argparse.Namespace:
    return argparse.Namespace(
        path=knowledge,
        uid=uid,
        entity_class=kwargs.get("entity_class", "person"),
        include_excluded=kwargs.get("include_excluded", False),
        usage_class=kwargs.get("usage_class", []),
    )


class TestCliEntityCommand:
    def test_prints_the_same_json_object_shape(
        self, corpus: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = cmd_entity(_entity_args(corpus, "alex", include_excluded=True))
        payload = json.loads(capsys.readouterr().out)

        assert rc == 0
        assert payload["uid"] == "alex"
        assert payload["contact"] == {"emails": ["alex@example.org"]}
        assert set(payload) == {
            "uid",
            "page_path",
            "frontmatter",
            "body",
            "contact",
            "redactions",
            "contact_included",
            "contact_record_path",
            "classifications",
            # athenaeum#851 — the per-value validity map (co-indexed with
            # `contact`, exactly as `classifications` is) and the per-record
            # do-not-email mark. Both are additive: every key above is
            # unchanged, so an existing consumer of this payload keeps working.
            "validity",
            "do_not_email",
        }

    def test_withheld_by_default_with_a_marker(
        self, corpus: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        cmd_entity(_entity_args(corpus, "alex"))
        payload = json.loads(capsys.readouterr().out)

        assert payload["contact"] == {}
        assert payload["redactions"] == [
            {"field": "emails", "value_count": 1, "redacted": True}
        ]

    def test_unknown_uid_exits_1(
        self, corpus: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = cmd_entity(_entity_args(corpus, "nobody"))

        assert rc == 1
        assert rc == EXIT_NOT_FOUND
        assert "no entity found" in capsys.readouterr().err

    def test_a_read_path_failure_after_the_uid_resolves_gets_a_different_code(
        self,
        corpus: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Issue athenaeum#1270: before this fix, ANY exception raised after
        the uid resolved (e.g. the ``json.dumps`` ``TypeError`` on an
        unserializable frontmatter value, issue athenaeum#1002 /
        athenaeum#1110) propagated uncaught and fell through to Python's
        default uncaught-exception exit status — which is ALSO `1`, identical
        to "uid not found". This forces that class of failure via a
        monkeypatched ``json.dumps`` (deliberately not the now-fixed date
        case — the whole point is that this must not be special-cased to
        dates) and pins that it now returns a DIFFERENT, non-1 code from the
        unknown-uid case above.
        """
        import athenaeum._cmd_query as query_mod

        def _boom(*_args: object, **_kwargs: object) -> str:
            raise TypeError("Object of type Sentinel is not JSON serializable")

        monkeypatch.setattr(query_mod.json, "dumps", _boom)

        rc = cmd_entity(_entity_args(corpus, "alex"))

        assert rc == EXIT_INTERNAL_ERROR
        assert rc != EXIT_NOT_FOUND
        err = capsys.readouterr().err
        assert "alex" in err
        assert "internal error" in err


def _subcommand_flags(name: str) -> set[str]:
    """Every option string registered on the ``athenaeum query <name>`` parser."""
    from athenaeum._cmd_query import add_query_subparsers

    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="cmd")
    add_query_subparsers(subparsers)
    sub = subparsers.choices[name]
    return {opt for action in sub._actions for opt in action.option_strings}


def _recall_args(knowledge: Path, query: str, **kwargs: object) -> argparse.Namespace:
    return argparse.Namespace(
        path=knowledge,
        query=query,
        top_k=kwargs.get("top_k", 5),
        cache_dir=knowledge / ".cache",
        backend="keyword",
        audience=kwargs.get("audience", None),
        as_of=None,
        with_pii=kwargs.get("with_pii", False),
        usage_class=kwargs.get("usage_class", []),
    )


class TestCliRecallWithPii:
    """`cmd_recall` is a SECOND implementation — its ordering is asserted here."""

    def test_default_performs_zero_excluded_scans(
        self, corpus: Path, capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def _explode(*args: object, **kwargs: object) -> list[Path]:
            raise AssertionError("default CLI recall must not scan the excluded surface")

        monkeypatch.setattr(pii, "iter_contact_records", _explode)

        assert cmd_recall(_recall_args(corpus, "widget")) == 0
        out = capsys.readouterr().out

        assert "alex.md" in out
        assert "alex@example.org" not in out

    def test_with_pii_appends_the_excluded_fields(
        self, corpus: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert cmd_recall(_recall_args(corpus, "widget", with_pii=True)) == 0
        out = capsys.readouterr().out

        assert "emails=alex@example.org" in out

    def test_usage_class_filter_is_honoured(
        self, corpus: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        cmd_recall(
            _recall_args(corpus, "widget", with_pii=True, usage_class=["observed"])
        )
        out = capsys.readouterr().out

        assert "alex@example.org" not in out

    def test_unauthorized_hit_gets_no_line_and_no_excluded_lookup(
        self, corpus: Path, capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Layer ordering, re-derived in THIS function and asserted against it."""

        def _explode(*args: object, **kwargs: object) -> list[Path]:
            raise AssertionError(
                "an unauthorized hit must never reach the excluded-surface lookup"
            )

        monkeypatch.setattr(pii, "iter_contact_records", _explode)

        rc = cmd_recall(
            _recall_args(corpus, "widget", with_pii=True, audience="secondary")
        )
        out = capsys.readouterr().out

        assert rc == 0
        assert "alex.md" not in out
        assert "alex@example.org" not in out

    def test_non_recallable_hit_gets_no_line_and_no_excluded_lookup(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        knowledge = tmp_path / "knowledge"
        knowledge.mkdir(parents=True)
        (knowledge / "athenaeum.yaml").write_text(
            "storage:\n"
            "  mapping:\n"
            "    pii: excluded\n"
            "    person: unrecallable\n"
            "  adapters:\n"
            "    unrecallable:\n"
            "      backing_store: wiki-markdown\n"
            "      surface_root: wiki\n"
            "      corpus_policy:\n"
            "        embedded: true\n"
            "        recallable: false\n"
            "        merge_eligible: false\n",
            encoding="utf-8",
        )
        _write_page(knowledge / "wiki", "alex", name="Alex Widget")
        _write_record(
            pii.contacts_surface_root(knowledge, EXCLUDED_CONFIG),
            "alex-contact.md",
            uid="alex",
            fields="emails:\n  - alex@example.org\n",
        )

        def _explode(*args: object, **kwargs: object) -> list[Path]:
            raise AssertionError(
                "a recallable:false hit must never reach the excluded-surface lookup"
            )

        monkeypatch.setattr(pii, "iter_contact_records", _explode)

        assert cmd_recall(_recall_args(knowledge, "widget", with_pii=True)) == 0
        out = capsys.readouterr().out

        assert "alex.md" not in out
        assert "alex@example.org" not in out

    def test_gate_refuses_a_class_whose_surface_is_not_excluded(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        knowledge = tmp_path / "knowledge"
        _write_config(knowledge)
        _write_page(
            knowledge / "wiki", "atlas", name="Atlas Widget", entity_type="project"
        )

        def _explode(*args: object, **kwargs: object) -> list[Path]:
            raise AssertionError("a non-excluded surface class must never be scanned")

        monkeypatch.setattr(pii, "iter_contact_records", _explode)

        assert cmd_recall(_recall_args(knowledge, "widget", with_pii=True)) == 0

        assert "atlas.md" in capsys.readouterr().out

    def test_hit_without_uid_joins_to_nothing(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        knowledge = tmp_path / "knowledge"
        _write_config(knowledge)
        wiki = knowledge / "wiki"
        wiki.mkdir(parents=True, exist_ok=True)
        (wiki / "loose.md").write_text(
            "---\nname: Loose Widget\ntype: person\n---\n\nA widget note.\n",
            encoding="utf-8",
        )

        def _explode(*args: object, **kwargs: object) -> list[Path]:
            raise AssertionError("a hit with no uid has nothing to join on")

        monkeypatch.setattr(pii, "iter_contact_records", _explode)

        assert cmd_recall(_recall_args(knowledge, "widget", with_pii=True)) == 0

        assert "loose.md" in capsys.readouterr().out

    def test_surface_is_scanned_once_for_many_hits(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        knowledge = tmp_path / "knowledge"
        _write_config(knowledge)
        contacts = pii.contacts_surface_root(knowledge, EXCLUDED_CONFIG)
        for uid in ("alex", "sam", "kim"):
            _write_page(knowledge / "wiki", uid, name=f"{uid.title()} Widget")
            _write_record(
                contacts,
                f"{uid}-contact.md",
                uid=uid,
                fields=f"emails:\n  - {uid}@example.org\n",
            )

        scans = 0
        original = pii.iter_contact_records

        def _counting(root: Path) -> list[Path]:
            nonlocal scans
            scans += 1
            return original(root)

        monkeypatch.setattr(pii, "iter_contact_records", _counting)

        cmd_recall(_recall_args(knowledge, "widget", with_pii=True))
        out = capsys.readouterr().out

        assert out.count("@example.org") == 3
        assert scans == 1


class TestExcludedFieldFlagsAreExactlyTheIntendedCommands:
    def test_the_commands_that_DO_gain_flags_are_exactly_the_intended_two(
        self,
    ) -> None:
        """What gained excluded-field flags at athenaeum#886, and only that.

        A third command, ``person``, also gained excluded-field flags at the
        same time as ``entity`` — it was removed in athenaeum#888 once every
        known consumer had migrated to ``entity``. A fourth command,
        ``people``, was deliberately NOT changed at athenaeum#886 (it is a
        separate multi-flag LISTING command over wiki frontmatter, not a uid
        read) and was later removed outright, unrelated to excluded-field
        flags, in athenaeum#1079. Only two remain here.
        """
        assert "--with-pii" in _subcommand_flags("recall")
        assert "--usage-class" in _subcommand_flags("recall")
        assert {"--include-excluded", "--class"} <= _subcommand_flags("entity")
