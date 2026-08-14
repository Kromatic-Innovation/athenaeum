# SPDX-License-Identifier: Apache-2.0
"""The MCP and CLI generic read surfaces (issue athenaeum#886).

athenaeum#883 made the primitives class-generic and athenaeum#885 wired `recall`;
after those, a caller reaching athenaeum over MCP or the shell could still only
ask the person-shaped question. These tests pin the two caller-facing surfaces:

- ``TestMcpRecallToolFlag`` — the `@mcp.tool() recall` tool takes `with_pii`
  and threads it; default behaviour for every existing caller is unchanged.
- ``TestMcpReadEntityTool`` — the generic tool carries ALL of `uid`, the
  entity class, `include_excluded` AND `usage_classes` (dropping the last is
  not a smaller version of the same tool — it removes a filter
  `docs/security-posture.md` §2.3 depends on), and its fail-closed audience
  check is the same one `person_read` applies.
- ``TestMcpReadPersonWrapperParity`` — the retained `read_person` tool's
  output is identical to the generic tool's across all four
  inclusion/record cells AND for a `usage_classes`-filtered case.
- ``TestCliEntityCommand`` / ``TestCliPersonWrapperParity`` — the same for the
  shell: `athenaeum query entity` prints the same JSON object shape, and
  `athenaeum query person` prints BYTE-IDENTICAL stdout to before, including
  a `--usage-class`-filtered case.
- ``TestCliRecallWithPii`` — `athenaeum recall --with-pii`. `cmd_recall` is a
  SECOND implementation of the layer ordering (it builds its own backend and
  its own authorization/recallable drops), so its layer ordering is asserted
  HERE against that code path — athenaeum#885's tests do not cover it.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from athenaeum import pii
from athenaeum._cmd_query import cmd_entity, cmd_person, cmd_recall
from athenaeum.mcp_server import entity_read, person_read

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
        """A restricted caller never receives a value, whichever tool it calls."""
        generic = entity_read(
            corpus,
            "alex",
            page_class="person",
            include_excluded=True,
            caller_audience=RESTRICTED,
            config=EXCLUDED_CONFIG,
        )
        person = person_read(
            corpus,
            "alex",
            include_contact_data=True,
            caller_audience=RESTRICTED,
            config=EXCLUDED_CONFIG,
        )

        assert "alex@example.org" not in generic
        assert "alex@example.org" not in person
        assert json.loads(generic) == json.loads(person)

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


class TestMcpReadPersonWrapperParity:
    """The retained tool's output is identical to the generic tool's."""

    @pytest.mark.parametrize("include", [True, False])
    @pytest.mark.parametrize("with_record", [True, False])
    def test_identical_output_across_the_four_cells(
        self, tmp_path: Path, include: bool, with_record: bool
    ) -> None:
        knowledge = tmp_path / "knowledge"
        _write_config(knowledge)
        _write_page(knowledge / "wiki", "alex", name="Alex Widget")
        if with_record:
            _write_record(
                pii.contacts_surface_root(knowledge, EXCLUDED_CONFIG),
                "alex-contact.md",
                uid="alex",
                fields="emails:\n  - alex@example.org\n",
            )
        server = _server(knowledge)
        person_tool = _tool(server, "read_person")
        entity_tool = _tool(server, "read_entity")

        assert person_tool.fn("alex", include) == entity_tool.fn("alex", "person", include)

    def test_identical_output_for_a_usage_class_filtered_case(
        self, corpus: Path
    ) -> None:
        server = _server(corpus)
        person_tool = _tool(server, "read_person")
        entity_tool = _tool(server, "read_entity")

        assert person_tool.fn("alex", True, ["observed"]) == entity_tool.fn(
            "alex", "person", True, ["observed"]
        )

    def test_read_person_tool_keeps_its_exact_argument_names(
        self, corpus: Path
    ) -> None:
        """Its name and ALL THREE arguments are unchanged (athenaeum#886 AC)."""
        tool = _tool(_server(corpus), "read_person")

        assert tool.name == "read_person"
        assert set(tool.parameters["properties"]) == {
            "uid",
            "include_contact_data",
            "usage_classes",
        }


def _person_args(knowledge: Path, uid: str, **kwargs: object) -> argparse.Namespace:
    return argparse.Namespace(
        path=knowledge,
        uid=uid,
        include_contact=kwargs.get("include_contact", False),
        usage_class=kwargs.get("usage_class", []),
    )


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
        assert "no entity found" in capsys.readouterr().err


class TestCliPersonWrapperParity:
    """`query person` prints BYTE-IDENTICAL stdout to the generic command."""

    @pytest.mark.parametrize("include", [True, False])
    def test_byte_identical_stdout(
        self, corpus: Path, capsys: pytest.CaptureFixture[str], include: bool
    ) -> None:
        assert cmd_person(_person_args(corpus, "alex", include_contact=include)) == 0
        person_out = capsys.readouterr().out
        assert cmd_entity(_entity_args(corpus, "alex", include_excluded=include)) == 0
        entity_out = capsys.readouterr().out

        assert person_out == entity_out

    def test_byte_identical_stdout_for_a_usage_class_filtered_case(
        self, corpus: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        cmd_person(
            _person_args(
                corpus, "alex", include_contact=True, usage_class=["observed"]
            )
        )
        person_out = capsys.readouterr().out
        cmd_entity(
            _entity_args(
                corpus, "alex", include_excluded=True, usage_class=["observed"]
            )
        )
        entity_out = capsys.readouterr().out

        assert person_out == entity_out

    def test_unknown_uid_keeps_its_exact_person_wording_and_exit_code(
        self, corpus: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = cmd_person(_person_args(corpus, "nobody"))

        assert rc == 1
        assert "no person found" in capsys.readouterr().err


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


class TestCmdPeopleIsUntouched:
    """`athenaeum query people` is deliberately NOT changed (athenaeum#886 out-of-scope).

    Confirmed explicitly rather than left ambiguous as to whether it was
    forgotten: it is a separate multi-flag LISTING command over wiki
    frontmatter, not a uid read, and it grows no excluded-field flag here.
    """

    def test_people_command_has_no_excluded_field_flags(self) -> None:
        flags = _subcommand_flags("people")

        assert not flags & {"--include-excluded", "--include-contact", "--with-pii"}

    def test_the_commands_that_DO_gain_flags_are_exactly_the_intended_three(
        self,
    ) -> None:
        """The complement of the assertion above — what did change, and only that."""
        assert "--with-pii" in _subcommand_flags("recall")
        assert "--usage-class" in _subcommand_flags("recall")
        assert {"--include-excluded", "--class"} <= _subcommand_flags("entity")
        # The retained person command keeps its own flag, unrenamed.
        assert "--include-contact" in _subcommand_flags("person")
