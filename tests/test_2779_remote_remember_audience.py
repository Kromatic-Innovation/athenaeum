# SPDX-License-Identifier: Apache-2.0
"""A remote ``remember`` is stamped with the caller's pinned audience (athenaeum#2779).

Before the stamp, a note written under ``--transport http --audience ops`` was
untagged, and an untagged page is withheld from every restricted audience, so
the caller that wrote it could never recall it. The stamp comes from the
server-side pin only; no ``remember`` argument and nothing in the note's own
frontmatter can change it.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from athenaeum.models import OWNER_ONLY_ACCESS_LEVELS, parse_frontmatter

NEEDLE = "Zarquon calibration protocol"
#: The note body, absent from a recall that echoes only the query.
BODY = "is quarterly"


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    knowledge = tmp_path / "knowledge"
    (knowledge / "wiki").mkdir(parents=True)
    (knowledge / "raw").mkdir(parents=True)
    return knowledge


def _server(corpus: Path, **kwargs: object):
    pytest.importorskip("fastmcp")
    from athenaeum.mcp_server import create_server

    return create_server(
        raw_root=corpus / "raw",
        wiki_root=corpus / "wiki",
        cache_dir=corpus.parent / "cache",
        search_backend="fts5",
        config={
            "search_backend": "fts5",
            "recall": {"extra_intake_roots": ["raw/remote-session", "raw/claude-session"]},
        },
        **kwargs,
    )


def _tool(server, name: str):
    async def _run():
        return (await server.get_tool(name)).fn

    return asyncio.run(_run())


def _remember(server, **kwargs: object) -> str:
    return _tool(server, "remember")(content=f"The {NEEDLE} is quarterly.", **kwargs)


def _recall(server) -> str:
    return _tool(server, "recall")(query=NEEDLE, top_k=5)


def _only_meta(corpus: Path, subdir: str) -> dict:
    files = sorted((corpus / "raw" / subdir).glob("*.md"))
    assert len(files) == 1
    meta, _ = parse_frontmatter(files[0].read_text(encoding="utf-8"))
    return meta


def _http(corpus: Path, audience: set[str] | None):
    return _server(corpus, transport="http", caller_audience=audience)


def _recall_as(corpus: Path, audience: set[str] | None, query: str) -> str:
    return _tool(_http(corpus, audience), "recall")(query=query, top_k=5)


class TestRemoteRememberStamp:
    def test_stamped_with_the_pinned_audience(self, corpus: Path) -> None:
        assert _remember(_http(corpus, {"ops"})).startswith("Saved to")
        assert _only_meta(corpus, "remote-session")["audience"] == ["ops"]

    def test_recallable_by_the_same_audience_in_the_same_session(self, corpus: Path) -> None:
        server = _http(corpus, {"ops"})
        _remember(server)
        assert BODY in _recall(server)

    def test_recallable_by_another_server_pinned_to_the_same_audience(
        self, corpus: Path
    ) -> None:
        _remember(_http(corpus, {"ops"}))
        assert BODY in _recall(_http(corpus, {"ops"}))

    def test_not_recallable_by_a_different_restricted_audience(self, corpus: Path) -> None:
        _remember(_http(corpus, {"ops"}))
        assert BODY not in _recall(_http(corpus, {"finance"}))

    def test_owner_pin_stamps_nothing_and_the_note_is_recallable(self, corpus: Path) -> None:
        """Owner (``caller_audience=None``) reads every page, so no stamp is needed."""
        server = _http(corpus, None)
        _remember(server)
        assert "audience" not in _only_meta(corpus, "remote-session")
        assert BODY in _recall(server)

    def test_multi_role_pin_stamps_every_role(self, corpus: Path) -> None:
        _remember(_http(corpus, {"ops", "sales"}))
        assert _only_meta(corpus, "remote-session")["audience"] == ["ops", "sales"]


class TestCallerCannotWidenTheStamp:
    def test_no_remember_argument_carries_an_audience(self, corpus: Path) -> None:
        server = _http(corpus, {"ops"})
        params = asyncio.run(server.get_tool("remember")).parameters["properties"]
        assert not {k for k in params if "audien" in k or k == "access"}

    def test_audience_in_the_note_frontmatter_is_replaced(self, corpus: Path) -> None:
        server = _http(corpus, {"ops"})
        _tool(server, "remember")(
            content=f"---\naudience: [finance, ops]\n---\nThe {NEEDLE} is quarterly.\n"
        )
        assert _only_meta(corpus, "remote-session")["audience"] == ["ops"]
        assert BODY not in _recall(_http(corpus, {"finance"}))

    def test_access_open_in_the_note_frontmatter_is_dropped(self, corpus: Path) -> None:
        server = _http(corpus, {"ops"})
        _tool(server, "remember")(
            content=f"---\naccess: open\n---\nThe {NEEDLE} is quarterly.\n"
        )
        meta = _only_meta(corpus, "remote-session")
        assert "access" not in meta
        assert meta["audience"] == ["ops"]
        assert BODY not in _recall(_http(corpus, {"finance"}))

    def test_a_caller_chosen_source_does_not_escape_the_stamp(self, corpus: Path) -> None:
        # The remember tool ignores a caller-supplied source and files the
        # note under the transport default (athenaeum#2764), so the note lands
        # in raw/remote-session/ and still carries the server's pinned stamp.
        _remember(_http(corpus, {"ops"}), source="elsewhere")
        assert not (corpus / "raw" / "elsewhere").exists()
        assert _only_meta(corpus, "remote-session")["audience"] == ["ops"]


class TestReplacedFrontmatterIsSignalled:
    """The reply and the log say when the server replaced the caller's frontmatter."""

    def _save(self, corpus: Path, frontmatter: str, audience: set[str] | None = None) -> str:
        server = _http(corpus, audience or {"ops"})
        return _tool(server, "remember")(
            content=f"---\n{frontmatter}\n---\nThe {NEEDLE} is quarterly.\n"
        )

    def test_a_replaced_audience_is_named_in_the_reply_and_the_log(
        self, corpus: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level("INFO", logger="athenaeum.mcp_server"):
            reply = self._save(corpus, "audience: [finance, ops]")
        first, _, rest = reply.partition("\n")
        assert first.startswith("Saved to raw/remote-session/")
        assert "Note: The note's own `audience:` was replaced by the server's" in rest
        assert any(
            r.levelname == "INFO" and "`audience:` was replaced" in r.getMessage()
            for r in caplog.records
        )

    def test_a_dropped_access_open_is_named_in_the_reply_and_the_log(
        self, corpus: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level("INFO", logger="athenaeum.mcp_server"):
            reply = self._save(corpus, "access: open")
        assert "`access: open` was dropped" in reply
        assert any("`access: open` was dropped" in r.getMessage() for r in caplog.records)

    def test_a_grant_dropped_from_a_restricted_note_is_named(self, corpus: Path) -> None:
        reply = self._save(corpus, "access: personal\naudience: [finance]")
        assert "`audience:` was dropped" in reply
        assert "owner-only" in reply

    def test_an_unchanged_note_gets_a_bare_reply(
        self, corpus: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Counter-examples: no frontmatter, or an audience equal to the pin, changes nothing."""
        with caplog.at_level("INFO", logger="athenaeum.mcp_server"):
            plain = _remember(_http(corpus, {"ops"}))
            same = self._save(corpus, "audience: [ops]")
        for reply in (plain, same):
            assert reply.startswith("Saved to ")
            assert "\n" not in reply
        assert not [r for r in caplog.records if "was replaced" in r.getMessage()]

    def test_owner_pin_signals_nothing(self, corpus: Path) -> None:
        server = _http(corpus, None)
        reply = _tool(server, "remember")(
            content=f"---\naudience: [finance]\n---\nThe {NEEDLE} is quarterly.\n"
        )
        assert "\n" not in reply


class TestNotesSavedBeforeTheStampAreResaved:
    """A note saved before the stamp existed is withheld; re-saving it is the fix.

    No command retags an existing raw note, so the supported step is to save the
    note again through the remote ``remember``. An owner-pinned save stamps
    nothing, which is exactly the shape a pre-stamp remote save has on disk.
    """

    def test_the_untagged_note_is_withheld_then_the_resave_is_recallable(
        self, corpus: Path
    ) -> None:
        _remember(_http(corpus, None))  # untagged, like a save from before the stamp
        (old,) = (corpus / "raw" / "remote-session").glob("*.md")
        old_text = old.read_text(encoding="utf-8")
        assert "audience" not in parse_frontmatter(old_text)[0]

        server = _http(corpus, {"ops"})
        assert BODY in _recall(_http(corpus, None))  # positive control: the owner sees it
        assert BODY not in _recall(server)  # withheld from the restricted audience

        assert _remember(server).startswith("Saved to")
        assert BODY in _recall(server)  # the re-save is recallable
        (new,) = (p for p in old.parent.glob("*.md") if p != old)
        assert parse_frontmatter(new.read_text(encoding="utf-8"))[0]["audience"] == ["ops"]
        assert old.read_text(encoding="utf-8") == old_text  # raw notes are write-once


class TestRestrictiveAccessIsNotWidened:
    """A restrictive ``access:`` is owner-only; the stamp must not open it to a role."""

    MEDICAL = "Owner was diagnosed with type 2 diabetes and started metformin."

    def test_a_screened_personal_note_is_not_stamped(self, corpus: Path) -> None:
        server = _server(
            corpus,
            transport="http",
            caller_audience={"ops"},
            screening={"medical": {"action": "label_restrict", "access": "personal"}},
        )
        assert _tool(server, "remember")(content=self.MEDICAL).startswith("Saved to")
        meta = _only_meta(corpus, "remote-session")
        assert meta["access"] == "personal"
        assert "audience" not in meta
        # Positive control: the owner reads it, so the query does find the note.
        assert "diagnosed" in _recall_as(corpus, None, "metformin")
        assert "diagnosed" not in _recall_as(corpus, {"ops"}, "metformin")

    @pytest.mark.parametrize("level", ["internal", "confidential", "personal"])
    def test_a_note_written_restrictive_drops_its_own_grant(
        self, corpus: Path, level: str
    ) -> None:
        _tool(_http(corpus, {"ops"}), "remember")(
            content=f"---\naccess: {level}\naudience: [finance]\n---\nThe {NEEDLE} is quarterly.\n"
        )
        meta = _only_meta(corpus, "remote-session")
        assert meta["access"] == level
        assert "audience" not in meta
        assert BODY not in _recall(_http(corpus, {"finance"}))
        assert BODY not in _recall(_http(corpus, {"ops"}))


class TestStdioUnchanged:
    def test_stdio_remember_stamps_no_audience_even_when_pinned(self, corpus: Path) -> None:
        server = _server(corpus, caller_audience={"ops"})
        _remember(server)
        assert "audience" not in _only_meta(corpus, "claude-session")

    def test_remember_write_without_a_pin_is_byte_identical(self, tmp_path: Path) -> None:
        from athenaeum.mcp_server import remember_write

        a, b = tmp_path / "a", tmp_path / "b"
        a.mkdir()
        b.mkdir()
        remember_write(a, "A fact.", "s", sources="user:x")
        remember_write(b, "A fact.", "s", sources="user:x", pinned_audience=None)
        (fa,) = (a / "s").glob("*.md")
        (fb,) = (b / "s").glob("*.md")
        assert fa.read_text().split("---", 2)[2:] == fb.read_text().split("---", 2)[2:]
        assert "audience" not in fa.read_text()


class TestOneOwnerOnlyDefinition:
    """``mcp_server`` and ``models.effective_audience`` share one definition (athenaeum#2924)."""

    def test_mcp_server_uses_the_models_definition(self) -> None:
        pytest.importorskip("fastmcp")
        from athenaeum import mcp_server, models

        assert mcp_server.OWNER_ONLY_ACCESS_LEVELS is models.OWNER_ONLY_ACCESS_LEVELS
        assert not hasattr(mcp_server, "_OWNER_ONLY_ACCESS_LEVELS")

    @pytest.mark.parametrize("level", sorted(OWNER_ONLY_ACCESS_LEVELS))
    def test_both_sites_treat_each_level_as_owner_only(self, level: str) -> None:
        pytest.importorskip("fastmcp")
        from athenaeum.mcp_server import _inject_provenance_frontmatter
        from athenaeum.models import effective_audience

        assert effective_audience({"access": level}) == (set(), False)
        out = _inject_provenance_frontmatter(
            f"---\naccess: {level}\n---\nbody\n", None, None, pinned_audience={"ops"}
        )
        assert "audience" not in parse_frontmatter(out)[0]
