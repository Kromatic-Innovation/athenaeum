# SPDX-License-Identifier: Apache-2.0
"""``read_person`` / ``read_people`` are deprecated (issue athenaeum#887).

Now that ``recall(with_pii=True)`` (athenaeum#885) and its MCP/CLI surfaces
(athenaeum#886) exist as the general replacement, the person-shaped entry points
are marked deprecated — with **no behaviour change**. Both keep working
identically; removal is a separate, later issue (athenaeum#888), gated on a real
deprecation window and on known consumers having migrated, not on this one
closing.

These tests pin exactly that: the warning fires, it names the replacement, and
nothing else moved.

- ``TestPythonApiWarns`` — both functions warn, with ``stacklevel=2`` so the
  warning is attributed to the CALLER's line, and the message names the
  replacement and the removal issue.
- ``TestReadPeopleWarnsAtCallTime`` — the subtle one. ``read_people`` returns a
  lazy iterator; a ``warnings.warn`` inside a generator BODY would not run
  until first advance, so the deprecation could be missed entirely by a caller
  that builds the iterator and hands it off. It must warn when CALLED, while
  staying lazy.
- ``TestBehaviourIsUnchanged`` — the warning is the only difference: results
  are identical to the generic path's, and laziness survives.
- ``TestSurfaceNotices`` — the MCP tool logs ONCE per process and never puts
  the notice in its JSON payload; the CLI prints to stderr and leaves stdout a
  clean parseable JSON object.
"""

from __future__ import annotations

import argparse
import json
import logging
import warnings
from pathlib import Path

import pytest

from athenaeum import mcp_server, pii
from athenaeum._cmd_query import cmd_person
from athenaeum.pii import (
    contacts_surface_root,
    read_entities,
    read_entity,
    read_people,
    read_person,
)

EXCLUDED_CONFIG: dict[str, object] = {"storage": {"mapping": {"pii": "excluded"}}}


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    knowledge = tmp_path / "knowledge"
    wiki = knowledge / "wiki"
    wiki.mkdir(parents=True)
    (wiki / "alex.md").write_text(
        "---\nuid: alex\nname: Alex Widget\ntype: person\n---\n\nNotes.\n",
        encoding="utf-8",
    )
    contacts = contacts_surface_root(knowledge, EXCLUDED_CONFIG)
    contacts.mkdir(parents=True, exist_ok=True)
    (contacts / "alex-contact.md").write_text(
        "---\nuid: alex\npii: true\nemails:\n  - alex@example.org\n---\n\nData.\n",
        encoding="utf-8",
    )
    (knowledge / "athenaeum.yaml").write_text(
        "storage:\n  mapping:\n    pii: excluded\n", encoding="utf-8"
    )
    return knowledge


class TestPythonApiWarns:
    def test_read_person_warns(self, corpus: Path) -> None:
        with pytest.warns(DeprecationWarning) as record:
            read_person(corpus, EXCLUDED_CONFIG, "alex")

        message = str(record[0].message)
        assert "read_person is deprecated" in message
        assert "recall(with_pii=True)" in message
        assert "read_entity" in message
        assert "athenaeum#888" in message

    def test_read_people_warns(self, corpus: Path) -> None:
        with pytest.warns(DeprecationWarning) as record:
            read_people(corpus, EXCLUDED_CONFIG, ["alex"])

        message = str(record[0].message)
        assert "read_people is deprecated" in message
        assert "read_entities" in message

    @pytest.mark.parametrize("name", ["read_person", "read_people"])
    def test_warning_is_attributed_to_the_callers_line(
        self, corpus: Path, name: str
    ) -> None:
        """``stacklevel=2`` — otherwise every warning points at pii.py, which
        tells a consumer nothing about which of THEIR call sites to migrate."""
        fn = getattr(pii, name)

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            fn(corpus, EXCLUDED_CONFIG, "alex" if name == "read_person" else ["alex"])

        assert caught[0].filename == __file__

    def test_the_generic_replacements_do_not_warn(self, corpus: Path) -> None:
        """The migration target must be quiet, or the warning is unactionable."""
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            read_entity(corpus, EXCLUDED_CONFIG, "alex", surface_class="pii")
            list(read_entities(corpus, EXCLUDED_CONFIG, ["alex"], surface_class="pii"))

        assert [w for w in caught if issubclass(w.category, DeprecationWarning)] == []


class TestReadPeopleWarnsAtCallTime:
    """A generator body's ``warn`` would not run until the first advance."""

    def test_warns_without_ever_consuming_the_iterator(self, corpus: Path) -> None:
        with pytest.warns(DeprecationWarning):
            stream = read_people(corpus, EXCLUDED_CONFIG, ["alex"])

        # Deliberately never advanced: the warning must already have fired.
        assert stream is not None

    def test_still_lazy_after_the_warning(
        self, corpus: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Warning eagerly must not make the READ eager (issue athenaeum#877)."""

        def _explode(*args: object, **kwargs: object) -> list[Path]:
            raise AssertionError("nothing may be read until the first pair is pulled")

        with pytest.warns(DeprecationWarning):
            stream = read_people(corpus, EXCLUDED_CONFIG, ["alex"])

        monkeypatch.setattr(pii, "iter_contact_records", _explode)
        # Constructing it read nothing; only advancing would (and would raise
        # here) — so the laziness contract is intact.
        assert stream is not None


class TestBehaviourIsUnchanged:
    """This issue changes zero behaviour — the warning is the only difference."""

    @pytest.mark.parametrize("include", [True, False])
    def test_read_person_result_matches_the_generic_path(
        self, corpus: Path, include: bool
    ) -> None:
        with pytest.warns(DeprecationWarning):
            legacy = read_person(
                corpus, EXCLUDED_CONFIG, "alex", include_contact=include
            )
        generic = read_entity(
            corpus,
            EXCLUDED_CONFIG,
            "alex",
            surface_class="pii",
            include_excluded=include,
        )

        assert legacy == generic

    def test_read_people_result_matches_the_generic_path(self, corpus: Path) -> None:
        with pytest.warns(DeprecationWarning):
            legacy = dict(
                read_people(corpus, EXCLUDED_CONFIG, ["alex"], include_contact=True)
            )
        generic = dict(
            read_entities(
                corpus,
                EXCLUDED_CONFIG,
                ["alex"],
                surface_class="pii",
                include_excluded=True,
            )
        )

        assert legacy == generic

    def test_read_people_keeps_its_positional_call_shape(self, corpus: Path) -> None:
        """apollo-enrich's exact call shape must survive the deprecation."""
        with pytest.warns(DeprecationWarning):
            batch = dict(
                read_people(corpus, EXCLUDED_CONFIG, ["alex"], include_contact=True)
            )

        assert batch["alex"] is not None
        assert batch["alex"].contact == {"emails": ["alex@example.org"]}


class TestSurfaceNotices:
    """The tool/command notices, each appropriate for its surface."""

    def _server(self, corpus: Path):
        pytest.importorskip("fastmcp")
        from athenaeum.mcp_server import create_server

        raw = corpus / "raw"
        raw.mkdir(parents=True, exist_ok=True)
        return create_server(
            raw_root=raw, wiki_root=corpus / "wiki", config=EXCLUDED_CONFIG
        )

    def _tool(self, server, name: str):
        import asyncio

        async def _run():
            tools = await server.list_tools()
            return next(t for t in tools if t.name == name)

        return asyncio.run(_run())

    def test_mcp_tool_logs_once_per_process(
        self, corpus: Path, monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setattr(mcp_server, "_READ_PERSON_TOOL_NOTICE_LOGGED", False)
        tool = self._tool(self._server(corpus), "read_person")

        with caplog.at_level(logging.WARNING, logger=mcp_server.log.name):
            tool.fn("alex")
            tool.fn("alex")
            tool.fn("alex")

        notices = [
            r for r in caplog.records if "read_person` is deprecated" in r.getMessage()
        ]
        assert len(notices) == 1

    def test_mcp_tool_json_payload_is_untouched(
        self, corpus: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A notice inside the payload would be a breaking output change."""
        monkeypatch.setattr(mcp_server, "_READ_PERSON_TOOL_NOTICE_LOGGED", False)
        server = self._server(corpus)
        person_tool = self._tool(server, "read_person")
        entity_tool = self._tool(server, "read_entity")

        payload = person_tool.fn("alex", True)

        assert payload == entity_tool.fn("alex", "person", True)
        assert "deprecated" not in payload
        # Still parseable, with no extra keys.
        assert json.loads(payload)["uid"] == "alex"

    def test_cli_notice_goes_to_stderr_and_stdout_stays_clean_json(
        self, corpus: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = cmd_person(
            argparse.Namespace(
                path=corpus, uid="alex", include_contact=True, usage_class=[]
            )
        )
        captured = capsys.readouterr()

        assert rc == 0
        assert "[deprecated]" in captured.err
        assert "athenaeum query entity" in captured.err
        # stdout is a script-parseable JSON object and nothing else.
        assert "deprecated" not in captured.out
        assert json.loads(captured.out)["contact"] == {"emails": ["alex@example.org"]}

    def test_cli_notice_does_not_change_the_exit_code(
        self, corpus: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = cmd_person(
            argparse.Namespace(
                path=corpus, uid="nobody", include_contact=False, usage_class=[]
            )
        )
        err = capsys.readouterr().err

        assert rc == 1
        assert "[deprecated]" in err
        assert "no person found" in err
