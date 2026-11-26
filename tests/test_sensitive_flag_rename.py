# SPDX-License-Identifier: Apache-2.0
"""One name for the sensitive-read flag (issue athenaeum#3163).

``include_sensitive`` is the one spelling on ``recall``, ``read_entity``,
``enumerate_entities``, ``athenaeum recall|entity|enumerate`` and the served
ceiling ``serve.sensitive_reads``. The old spellings (``with_pii``,
``include_excluded`` and their flags, ``serve.excluded_fields``'s CLI/env
forms) keep working for one minor release with a notice; the old yaml key is
refused.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

import pytest

from athenaeum import pii
from athenaeum._cmd_enumerate import cmd_enumerate
from athenaeum._cmd_query import cmd_entity, cmd_recall
from athenaeum.config import (
    _REMOVED_CONFIG_KEYS,
    find_removed_config_keys,
    resolve_serve_sensitive_reads,
)
from athenaeum.remote_policy import RemotePolicyError
from athenaeum.sensitivity_routing import resolve_pointers_in_text, route_sensitive_values

EXCLUDED_CONFIG: dict[str, object] = {"storage": {"mapping": {"pii": "excluded"}}}
EMAIL = "alex@example.org"


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    knowledge = tmp_path / "knowledge"
    wiki = knowledge / "wiki"
    wiki.mkdir(parents=True)
    (knowledge / "athenaeum.yaml").write_text(
        "storage:\n  mapping:\n    pii: excluded\n", encoding="utf-8"
    )
    (wiki / "alex.md").write_text(
        "---\nuid: alex\nname: Alex Widget\ntype: person\n---\n\n"
        "Alex Widget works on widget calibration.\n",
        encoding="utf-8",
    )
    root = pii.contacts_surface_root(knowledge, EXCLUDED_CONFIG)
    root.mkdir(parents=True, exist_ok=True)
    (root / "alex-contact.md").write_text(
        f"---\nuid: alex\npii: true\nemails:\n  - {EMAIL}\n---\n\nArchival data.\n",
        encoding="utf-8",
    )
    return knowledge


def _server(knowledge: Path):
    pytest.importorskip("fastmcp")
    from athenaeum.mcp_server import create_server

    raw = knowledge / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    return create_server(raw_root=raw, wiki_root=knowledge / "wiki", config=EXCLUDED_CONFIG)


def _tool(server, name: str):
    async def _run():
        return next(t for t in await server.list_tools() if t.name == name)

    return asyncio.run(_run())


class TestReconcile:
    def test_new_name_alone_has_no_notice(self) -> None:
        assert pii.reconcile_sensitive_flag("include_sensitive", True, {"with_pii": None}) == (
            True,
            [],
        )

    def test_unset_is_false(self) -> None:
        assert pii.reconcile_sensitive_flag("include_sensitive", None, {"with_pii": None}) == (
            False,
            [],
        )

    def test_old_name_behaves_the_same_and_names_the_replacement(self) -> None:
        value, notices = pii.reconcile_sensitive_flag(
            "include_sensitive", None, {"with_pii": True}
        )
        assert value is True
        assert len(notices) == 1
        assert "`with_pii`" in notices[0] and "`include_sensitive`" in notices[0]

    def test_old_false_is_honoured(self) -> None:
        value, _ = pii.reconcile_sensitive_flag("include_sensitive", None, {"with_pii": False})
        assert value is False

    def test_same_values_agree(self) -> None:
        value, notices = pii.reconcile_sensitive_flag(
            "include_sensitive", True, {"with_pii": True}
        )
        assert value is True and len(notices) == 1

    def test_different_values_are_an_error(self) -> None:
        with pytest.raises(ValueError, match="different values"):
            pii.reconcile_sensitive_flag("include_sensitive", False, {"with_pii": True})


class TestMcpRecall:
    def test_new_name_returns_values_without_a_notice(self, corpus: Path) -> None:
        out = _tool(_server(corpus), "recall").fn("widget", include_sensitive=True)
        assert EMAIL in out
        assert "deprecated" not in out.lower()

    def test_old_name_is_identical_plus_one_notice(self, corpus: Path) -> None:
        tool = _tool(_server(corpus), "recall")
        new = tool.fn("widget", include_sensitive=True)
        old = tool.fn("widget", with_pii=True)
        assert old.startswith(new)
        assert old.count("deprecated") == 1
        assert "`include_sensitive`" in old[len(new) :]

    def test_old_name_on_the_json_path_carries_the_notice_key(self, corpus: Path) -> None:
        out = _tool(_server(corpus), "recall").fn(EMAIL, with_pii=True)
        payload = json.loads(out)
        assert "include_sensitive" in payload["deprecation_notice"]

    def test_conflicting_values_are_an_error(self, corpus: Path) -> None:
        with pytest.raises(ValueError, match="different values"):
            _tool(_server(corpus), "recall").fn(
                "widget", include_sensitive=False, with_pii=True
            )

    def test_description_and_parameter_agree(self, corpus: Path) -> None:
        tool = _tool(_server(corpus), "recall")
        assert "include_sensitive" in tool.parameters["properties"]
        described = tool.parameters["properties"]["include_sensitive"].get("description", "")
        assert "SENSITIVE" in described or "sensitive" in described


class TestMcpReadEntity:
    def _read(self, corpus: Path, **kwargs: object) -> dict:
        return json.loads(_tool(_server(corpus), "read_entity").fn("alex", "person", **kwargs))

    def test_new_name_returns_values(self, corpus: Path) -> None:
        payload = self._read(corpus, include_sensitive=True)
        assert EMAIL in json.dumps(payload["contact"])
        assert "deprecation_notice" not in payload

    def test_default_withholds(self, corpus: Path) -> None:
        assert EMAIL not in json.dumps(self._read(corpus))

    def test_old_name_is_identical_plus_one_notice(self, corpus: Path) -> None:
        old = self._read(corpus, include_excluded=True)
        new = self._read(corpus, include_sensitive=True)
        notice = old.pop("deprecation_notice")
        assert "include_sensitive" in notice and "include_excluded" in notice
        assert old == new

    def test_conflicting_values_are_an_error(self, corpus: Path) -> None:
        with pytest.raises(ValueError, match="different values"):
            self._read(corpus, include_sensitive=True, include_excluded=False)


class TestMcpEnumerate:
    def test_old_name_gets_a_notice_and_new_name_does_not(self, corpus: Path) -> None:
        tool = _tool(_server(corpus), "enumerate_entities")
        assert "deprecation_notice" not in tool.fn("person", include_sensitive=True)
        out = tool.fn("person", with_pii=True)
        assert "include_sensitive" in out["deprecation_notice"]

    def test_conflict_is_reported_as_an_error(self, corpus: Path) -> None:
        out = _tool(_server(corpus), "enumerate_entities").fn(
            "person", include_sensitive=False, with_pii=True
        )
        assert "different values" in out["error"]

    def test_gated_field_error_names_the_new_flag(self, corpus: Path) -> None:
        out = _tool(_server(corpus), "enumerate_entities").fn(
            "person", fields=["google_contact_x"]
        )
        assert "include_sensitive" in out["error"]
        assert "with_pii" not in out["error"]


class TestCli:
    def _recall_ns(self, knowledge: Path, **kw: object) -> argparse.Namespace:
        return argparse.Namespace(
            path=knowledge,
            query="widget",
            top_k=5,
            cache_dir=knowledge / ".cache",
            backend="keyword",
            audience=None,
            as_of=None,
            usage_class=[],
            include_sensitive=kw.get("include_sensitive", False),
            with_pii=kw.get("with_pii", False),
        )

    def test_recall_new_flag(self, corpus: Path, capsys: pytest.CaptureFixture[str]) -> None:
        assert cmd_recall(self._recall_ns(corpus, include_sensitive=True)) == 0
        captured = capsys.readouterr()
        assert EMAIL in captured.out and captured.err == ""

    def test_recall_old_flag_one_notice(
        self, corpus: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert cmd_recall(self._recall_ns(corpus, with_pii=True)) == 0
        captured = capsys.readouterr()
        assert EMAIL in captured.out
        assert captured.err.count("--with-pii") == 1
        assert "--include-sensitive" in captured.err

    def _entity_ns(self, knowledge: Path, **kw: object) -> argparse.Namespace:
        return argparse.Namespace(
            path=knowledge,
            uid="alex",
            entity_class="person",
            usage_class=[],
            include_sensitive=kw.get("include_sensitive", False),
            include_excluded=kw.get("include_excluded", False),
        )

    def test_entity_new_and_old_flag(
        self, corpus: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert cmd_entity(self._entity_ns(corpus, include_sensitive=True)) == 0
        new = capsys.readouterr()
        assert EMAIL in new.out and new.err == ""
        assert cmd_entity(self._entity_ns(corpus, include_excluded=True)) == 0
        old = capsys.readouterr()
        assert old.out == new.out
        assert old.err.count("--include-excluded") == 1
        assert "--include-sensitive" in old.err

    def test_enumerate_old_flag_notice(
        self, corpus: Path, capsys: pytest.CaptureFixture[str], tmp_path: Path
    ) -> None:
        def ns(**kw: object) -> argparse.Namespace:
            return argparse.Namespace(
                entity_type="person",
                where=[],
                sort_key="name",
                ascending=False,
                limit=50,
                cursor=None,
                field=["google_contact_x"],
                include_split=False,
                audience=None,
                path=corpus,
                cache_dir=tmp_path / "cache",
                include_sensitive=kw.get("include_sensitive", False),
                with_pii=kw.get("with_pii", False),
            )

        assert cmd_enumerate(ns(include_sensitive=True)) == 0
        assert capsys.readouterr().err == ""
        assert cmd_enumerate(ns(with_pii=True)) == 0
        assert capsys.readouterr().err.count("--with-pii") == 1
        assert cmd_enumerate(ns()) == 1
        assert "include_sensitive" in capsys.readouterr().err

    def test_parsers_accept_both_spellings(self) -> None:
        from athenaeum.cli import build_parser

        parser = build_parser()
        for argv in (
            ["recall", "x", "--include-sensitive"],
            ["recall", "x", "--with-pii"],
            ["entity", "--uid", "a", "--include-sensitive"],
            ["entity", "--uid", "a", "--include-excluded"],
            ["enumerate", "--type", "person", "--include-sensitive"],
            ["enumerate", "--type", "person", "--with-pii"],
        ):
            parser.parse_args(argv)


class TestServeCeiling:
    @pytest.fixture(autouse=True)
    def _clean_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ATHENAEUM_SERVE_SENSITIVE_READS", raising=False)
        monkeypatch.delenv("ATHENAEUM_SERVE_EXCLUDED_FIELDS", raising=False)

    def test_yaml_key(self) -> None:
        assert resolve_serve_sensitive_reads({"serve": {"sensitive_reads": "off"}}) is False
        assert resolve_serve_sensitive_reads({"serve": {"sensitive_reads": "on"}}) is True
        assert resolve_serve_sensitive_reads({}) is None

    def test_old_yaml_key_is_refused_with_the_replacement(self) -> None:
        with pytest.raises(RemotePolicyError, match=r"serve\.sensitive_reads"):
            resolve_serve_sensitive_reads({"serve": {"excluded_fields": "off"}})

    def test_old_key_is_registered_as_removed(self) -> None:
        assert "serve.excluded_fields" in _REMOVED_CONFIG_KEYS
        found = find_removed_config_keys({"serve": {"excluded_fields": "on"}})
        assert [k for k, _ in found] == ["serve.excluded_fields"]
        assert "serve.sensitive_reads" in found[0][1]

    def test_env_new_and_old(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("ATHENAEUM_SERVE_SENSITIVE_READS", "off")
        assert resolve_serve_sensitive_reads({}) is False
        assert capsys.readouterr().err == ""
        monkeypatch.delenv("ATHENAEUM_SERVE_SENSITIVE_READS")
        monkeypatch.setenv("ATHENAEUM_SERVE_EXCLUDED_FIELDS", "off")
        assert resolve_serve_sensitive_reads({}) is False
        err = capsys.readouterr().err
        assert err.count("ATHENAEUM_SERVE_EXCLUDED_FIELDS") == 1
        assert "ATHENAEUM_SERVE_SENSITIVE_READS" in err

    def test_cli_old_flag_notice_and_conflict(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert resolve_serve_sensitive_reads({}, None, "off") is False
        assert "--sensitive-reads" in capsys.readouterr().err
        with pytest.raises(RemotePolicyError, match="different"):
            resolve_serve_sensitive_reads({}, "on", "off")

    def test_env_conflict(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATHENAEUM_SERVE_SENSITIVE_READS", "on")
        monkeypatch.setenv("ATHENAEUM_SERVE_EXCLUDED_FIELDS", "off")
        with pytest.raises(RemotePolicyError, match="different"):
            resolve_serve_sensitive_reads({})

    def test_serve_parser_has_both_flags(self) -> None:
        from athenaeum.cli import build_parser

        ns = build_parser().parse_args(["serve", "--sensitive-reads", "off"])
        assert ns.sensitive_reads == "off"
        ns = build_parser().parse_args(["serve", "--excluded-fields", "off"])
        assert ns.excluded_fields == "off"


class TestAgentFacingCopy:
    def test_remote_instructions_use_the_new_name_only(self) -> None:
        from athenaeum.mcp_server import remote_server_instructions

        text = remote_server_instructions()
        assert "include_sensitive" in text
        assert "with_pii" not in text and "include_excluded" not in text

    def test_read_entity_description_uses_the_new_name(self) -> None:
        from athenaeum.mcp_server import read_entity_tool_docstring

        doc = read_entity_tool_docstring()
        assert "include_sensitive" in doc
        assert "recall(include_sensitive=True)" in doc


class TestPointerResolution:
    """AC5: the entity read resolves a routed-value pointer under the same flag."""

    ROUTING = {"sensitivity": {"routing": {"enabled": True}}}

    def _page_with_pointer(self, knowledge: Path) -> str:
        pointered = route_sensitive_values(
            raw_ref="src/a.md",
            text=f"Reach {EMAIL} please.",
            frontmatter={},
            config=self.ROUTING,
            knowledge_root=knowledge,
        )
        assert EMAIL not in pointered
        wiki = knowledge / "wiki"
        wiki.mkdir(parents=True, exist_ok=True)
        (wiki / "carol.md").write_text(
            f"---\nuid: carol\nname: Carol\ntype: person\n---\n\n{pointered}\n", encoding="utf-8"
        )
        return pointered

    def test_pointer_text_names_a_callable_read(self, tmp_path: Path) -> None:
        pointered = self._page_with_pointer(tmp_path)
        assert "resolve_sensitive_record" not in pointered
        assert "include_sensitive=true" in pointered

    def test_read_entity_resolves_pointer_only_with_the_flag(self, tmp_path: Path) -> None:
        from athenaeum.mcp_server import entity_read

        self._page_with_pointer(tmp_path)
        withheld = json.loads(
            entity_read(tmp_path, "carol", page_class="person", config=self.ROUTING)
        )
        assert EMAIL not in withheld["body"]
        shown = json.loads(
            entity_read(
                tmp_path,
                "carol",
                page_class="person",
                config=self.ROUTING,
                include_excluded=True,
            )
        )
        assert EMAIL in shown["body"]

    def test_the_serve_pin_keeps_pointers_unresolved(self, tmp_path: Path) -> None:
        from athenaeum.mcp_server import entity_read

        self._page_with_pointer(tmp_path)
        pinned = json.loads(
            entity_read(
                tmp_path,
                "carol",
                page_class="person",
                config=self.ROUTING,
                include_excluded=True,
                sensitive_reads=False,
            )
        )
        assert EMAIL not in pinned["body"]

    def test_legacy_pointer_wording_still_resolves(self, tmp_path: Path) -> None:
        pointered = self._page_with_pointer(tmp_path)
        legacy = pointered.replace(
            "read this page with include_sensitive=true to resolve",
            "resolve via athenaeum.sensitivity_routing.resolve_sensitive_record()",
        )
        assert legacy != pointered
        assert EMAIL in resolve_pointers_in_text(legacy, self.ROUTING, tmp_path)

    def test_unresolvable_pointer_is_left_in_place(self, tmp_path: Path) -> None:
        bogus = (
            f"[sensitive:pii:{'a' * 32} — value withheld; "
            "read this page with include_sensitive=true to resolve]"
        )
        assert resolve_pointers_in_text(bogus, self.ROUTING, tmp_path) == bogus
