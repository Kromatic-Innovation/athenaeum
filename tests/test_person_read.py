# SPDX-License-Identifier: Apache-2.0
"""Tests for the one-call person read (issue athenaeum#864).

Structure mirrors the issue's acceptance criteria — a single entry point
(:func:`athenaeum.pii.read_person`) that returns a person page by ``uid``,
with a boolean controlling contact-data inclusion (default excluded), a
redaction marker distinguishing "withheld" from "absent", and reachability
from both the MCP server (:func:`athenaeum.mcp_server.person_read`, the
``read_person`` tool's helper) and the ``athenaeum query person`` CLI.

- ``TestReadPersonFourCells`` — the four cells: {include on, include off} x
  {contact record present, absent} — including the explicit "flag-off
  differs by record presence" assertion the issue calls out.
- ``TestContactDataFieldsUnion`` — a ``former_emails``/``alt_emails``-only
  record is redacted/returned too (the union field set).
- ``TestUnknownUid`` — ``None`` from ``read_person``; exit code 1 from the CLI.
- ``TestCallerNeverConstructsSurfacePath`` — the record resolves under
  ``contacts_surface_root``, and ``read_person`` is given only a ``uid``.
- ``TestResolveContactRecordForUidDeterminism`` — two records sharing a
  ``uid`` resolve to the deterministic first (mirrors
  ``tests/test_bounce_identifier_resolution.py``'s multi-match test style).
- ``TestCliPersonCommand`` — the shell surface: parseable JSON in both flag
  states, exit code 1 for an unknown uid.
- ``TestMcpPersonRead`` — the MCP surface: same information as
  ``pii.read_person``, and fail-closed (with NO contact value in the
  refusal) for a restricted ``caller_audience`` on an unauthorized page.

Fixtures follow the ``EXCLUDED_CONFIG`` + tmp-path corpus-builder idiom from
``tests/test_pii_off_corpus.py`` / ``tests/test_bounce_mark.py`` rather than
inventing new ones. Ordinary ``alex@example.org``-style addresses only — no
``+alias`` shape, which is suppressed for other test files but NOT for this
one in ``.public-safe-lintignore``.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from athenaeum.pii import (
    CONTACT_DATA_FIELDS,
    RedactionMarker,
    contacts_surface_root,
    read_person,
    resolve_contact_record_for_uid,
)

EXCLUDED_CONFIG = {"storage": {"mapping": {"pii": "excluded"}}}


def _write_wiki_person(
    wiki_root: Path, uid: str, *, name: str = "Alex Example", extra: str = ""
) -> Path:
    """Write a minimal ``type: person`` wiki page indexable by ``EntityIndex``."""
    wiki_root.mkdir(parents=True, exist_ok=True)
    path = wiki_root / f"{uid}.md"
    path.write_text(
        f"---\nuid: {uid}\nname: {name}\ntype: person\n{extra}---\n\n"
        f"Notes about {name}.\n",
        encoding="utf-8",
    )
    return path


def _write_contact_record(
    contacts_root: Path, filename: str, *, uid: str, fields: str = ""
) -> Path:
    """Write a synthetic contact record on the (excluded) contacts surface."""
    contacts_root.mkdir(parents=True, exist_ok=True)
    path = contacts_root / filename
    path.write_text(
        f"---\nuid: {uid}\npii: true\n{fields}---\n\nArchival contact data.\n",
        encoding="utf-8",
    )
    return path


# ---------------------------------------------------------------------------
# The four cells
# ---------------------------------------------------------------------------


class TestReadPersonFourCells:
    def test_include_off_record_present_redacts(self, tmp_path: Path) -> None:
        knowledge = tmp_path / "knowledge"
        _write_wiki_person(knowledge / "wiki", "alex")
        contacts_root = contacts_surface_root(knowledge, EXCLUDED_CONFIG)
        _write_contact_record(
            contacts_root,
            "alex-contact.md",
            uid="alex",
            fields="emails:\n  - alex@example.org\n",
        )

        result = read_person(knowledge, EXCLUDED_CONFIG, "alex", include_contact=False)

        assert result is not None
        assert result.contact == {}
        assert result.contact_included is False
        assert result.redactions == (RedactionMarker(field="emails", value_count=1),)
        assert result.contact_record_path is not None

    def test_include_off_no_record_no_markers(self, tmp_path: Path) -> None:
        knowledge = tmp_path / "knowledge"
        _write_wiki_person(knowledge / "wiki", "alex")
        # No contact record written at all.

        result = read_person(knowledge, EXCLUDED_CONFIG, "alex", include_contact=False)

        assert result is not None
        assert result.contact == {}
        assert result.redactions == ()
        assert result.contact_record_path is None

    def test_include_on_record_present_returns_values(self, tmp_path: Path) -> None:
        knowledge = tmp_path / "knowledge"
        _write_wiki_person(knowledge / "wiki", "alex")
        contacts_root = contacts_surface_root(knowledge, EXCLUDED_CONFIG)
        _write_contact_record(
            contacts_root,
            "alex-contact.md",
            uid="alex",
            fields="emails:\n  - alex@example.org\nphones:\n  - +1-555-0100\n",
        )

        result = read_person(knowledge, EXCLUDED_CONFIG, "alex", include_contact=True)

        assert result is not None
        assert result.contact == {
            "emails": ["alex@example.org"],
            "phones": ["+1-555-0100"],
        }
        assert result.contact_included is True
        assert result.redactions == ()

    def test_include_on_no_record_returns_nothing(self, tmp_path: Path) -> None:
        knowledge = tmp_path / "knowledge"
        _write_wiki_person(knowledge / "wiki", "alex")

        result = read_person(knowledge, EXCLUDED_CONFIG, "alex", include_contact=True)

        assert result is not None
        assert result.contact == {}
        assert result.redactions == ()
        assert result.contact_record_path is None

    def test_flag_off_with_and_without_record_are_different_responses(
        self, tmp_path: Path
    ) -> None:
        # The whole point of the redaction marker: a person with an email on
        # the excluded surface and a person with no email produce DIFFERENT
        # responses even though neither carries the value.
        knowledge = tmp_path / "knowledge"
        wiki_root = knowledge / "wiki"
        _write_wiki_person(wiki_root, "alex", name="Alex Has Email")
        _write_wiki_person(wiki_root, "sam", name="Sam Has No Email")
        contacts_root = contacts_surface_root(knowledge, EXCLUDED_CONFIG)
        _write_contact_record(
            contacts_root,
            "alex-contact.md",
            uid="alex",
            fields="emails:\n  - alex@example.org\n",
        )

        with_email = read_person(knowledge, EXCLUDED_CONFIG, "alex", include_contact=False)
        without_email = read_person(knowledge, EXCLUDED_CONFIG, "sam", include_contact=False)

        assert with_email is not None and without_email is not None
        assert with_email.contact == without_email.contact == {}
        assert with_email.redactions != without_email.redactions
        assert with_email.redactions == (RedactionMarker(field="emails", value_count=1),)
        assert without_email.redactions == ()
        assert with_email.to_dict() != without_email.to_dict()


# ---------------------------------------------------------------------------
# CONTACT_DATA_FIELDS union — former_emails / alt_emails
# ---------------------------------------------------------------------------


class TestContactDataFieldsUnion:
    def test_union_is_frontmatter_plus_identifier_fields_in_order(self) -> None:
        assert CONTACT_DATA_FIELDS == ("emails", "phones", "former_emails", "alt_emails")

    @pytest.mark.parametrize("field", ["former_emails", "alt_emails"])
    def test_former_or_alt_emails_only_record_is_redacted(
        self, tmp_path: Path, field: str
    ) -> None:
        knowledge = tmp_path / "knowledge"
        _write_wiki_person(knowledge / "wiki", "sam")
        contacts_root = contacts_surface_root(knowledge, EXCLUDED_CONFIG)
        _write_contact_record(
            contacts_root,
            "sam-contact.md",
            uid="sam",
            fields=f"{field}:\n  - sam.old@example.org\n",
        )

        redacted = read_person(knowledge, EXCLUDED_CONFIG, "sam", include_contact=False)
        included = read_person(knowledge, EXCLUDED_CONFIG, "sam", include_contact=True)

        assert redacted is not None and included is not None
        assert redacted.contact == {}
        assert redacted.redactions == (RedactionMarker(field=field, value_count=1),)
        assert included.contact == {field: ["sam.old@example.org"]}
        assert included.redactions == ()


# ---------------------------------------------------------------------------
# Unknown uid
# ---------------------------------------------------------------------------


class TestUnknownUid:
    def test_read_person_returns_none(self, tmp_path: Path) -> None:
        knowledge = tmp_path / "knowledge"
        (knowledge / "wiki").mkdir(parents=True)
        assert read_person(knowledge, EXCLUDED_CONFIG, "no-such-uid") is None

    def test_cli_unknown_uid_exits_1(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from athenaeum._cmd_query import cmd_person

        knowledge = tmp_path / "knowledge"
        (knowledge / "wiki").mkdir(parents=True)
        args = argparse.Namespace(
            uid="no-such-uid", include_contact=False, usage_class=[], path=knowledge
        )

        rc = cmd_person(args)

        assert rc == 1
        assert "no-such-uid" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# The caller never constructs the surface path
# ---------------------------------------------------------------------------


class TestCallerNeverConstructsSurfacePath:
    def test_record_resolves_under_contacts_surface_root(self, tmp_path: Path) -> None:
        knowledge = tmp_path / "knowledge"
        _write_wiki_person(knowledge / "wiki", "alex")
        contacts_root = contacts_surface_root(knowledge, EXCLUDED_CONFIG)
        record = _write_contact_record(
            contacts_root,
            "alex-contact.md",
            uid="alex",
            fields="emails:\n  - alex@example.org\n",
        )

        # read_person's caller supplies only knowledge_root/config/uid — never
        # a path onto the contacts surface.
        result = read_person(knowledge, EXCLUDED_CONFIG, "alex", include_contact=True)

        assert result is not None
        assert result.contact_record_path == record
        assert result.contact_record_path.is_relative_to(contacts_root)


# ---------------------------------------------------------------------------
# Determinism / multi-match (mirrors test_bounce_identifier_resolution.py)
# ---------------------------------------------------------------------------


class TestResolveContactRecordForUidDeterminism:
    def test_two_records_same_uid_resolves_to_first(self, tmp_path: Path) -> None:
        contacts_root = tmp_path / "contacts"
        contacts_root.mkdir(parents=True)
        for stem in ("b-second", "a-first"):
            (contacts_root / f"{stem}.md").write_text(
                "---\nuid: shared-uid\npii: true\nemails:\n  - shared@example.org\n---\n\nx\n",
                encoding="utf-8",
            )

        first = resolve_contact_record_for_uid(contacts_root, "shared-uid")

        assert first is not None and first.name == "a-first.md"
        assert resolve_contact_record_for_uid(contacts_root, "shared-uid") == first

    def test_blank_uid_never_matches(self, tmp_path: Path) -> None:
        contacts_root = tmp_path / "contacts"
        contacts_root.mkdir(parents=True)
        (contacts_root / "no-uid.md").write_text(
            "---\npii: true\nemails:\n  - x@example.org\n---\n\nx\n", encoding="utf-8"
        )
        assert resolve_contact_record_for_uid(contacts_root, "") is None
        assert resolve_contact_record_for_uid(contacts_root, "   ") is None

    def test_missing_surface_resolves_to_nothing(self, tmp_path: Path) -> None:
        assert resolve_contact_record_for_uid(tmp_path / "absent", "alex") is None


# ---------------------------------------------------------------------------
# CLI: `athenaeum query person --uid ... [--include-contact]`
# ---------------------------------------------------------------------------


class TestCliPersonCommand:
    def _run(
        self,
        knowledge_root: Path,
        uid: str,
        include_contact: bool,
        capsys: pytest.CaptureFixture[str],
    ) -> tuple[int, str]:
        from athenaeum._cmd_query import cmd_person

        args = argparse.Namespace(
            uid=uid,
            include_contact=include_contact,
            # `--usage-class` defaults to [] in the real parser (issue
            # athenaeum#866); [] means "no class filter", not "no values".
            usage_class=[],
            path=knowledge_root,
        )
        rc = cmd_person(args)
        return rc, capsys.readouterr().out

    def _build_knowledge(self, tmp_path: Path) -> Path:
        knowledge = tmp_path / "knowledge"
        _write_wiki_person(knowledge / "wiki", "alex")
        (knowledge / "athenaeum.yaml").write_text(
            "storage:\n  mapping:\n    pii: excluded\n", encoding="utf-8"
        )
        contacts_root = contacts_surface_root(knowledge, EXCLUDED_CONFIG)
        _write_contact_record(
            contacts_root,
            "alex-contact.md",
            uid="alex",
            fields="emails:\n  - alex@example.org\n",
        )
        return knowledge

    def test_cli_emits_parseable_json_include_off(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        knowledge = self._build_knowledge(tmp_path)
        rc, out = self._run(knowledge, "alex", False, capsys)

        assert rc == 0
        payload = json.loads(out)
        assert payload["uid"] == "alex"
        assert payload["contact"] == {}
        assert payload["redactions"] == [
            {"field": "emails", "value_count": 1, "redacted": True}
        ]
        assert "alex@example.org" not in out

    def test_cli_emits_parseable_json_include_on(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        knowledge = self._build_knowledge(tmp_path)
        rc, out = self._run(knowledge, "alex", True, capsys)

        assert rc == 0
        payload = json.loads(out)
        assert payload["contact"] == {"emails": ["alex@example.org"]}
        assert payload["redactions"] == []


# ---------------------------------------------------------------------------
# MCP surface: athenaeum.mcp_server.person_read
# ---------------------------------------------------------------------------


class TestMcpPersonRead:
    RESTRICTED = {"secondary"}

    def _build_knowledge(
        self, tmp_path: Path, *, page_extra: str = ""
    ) -> Path:
        knowledge = tmp_path / "knowledge"
        _write_wiki_person(knowledge / "wiki", "alex", extra=page_extra)
        contacts_root = contacts_surface_root(knowledge, EXCLUDED_CONFIG)
        _write_contact_record(
            contacts_root,
            "alex-contact.md",
            uid="alex",
            fields="emails:\n  - alex@example.org\n",
        )
        return knowledge

    def test_owner_matches_read_person(self, tmp_path: Path) -> None:
        from athenaeum.mcp_server import person_read

        knowledge = self._build_knowledge(tmp_path)
        expected = read_person(knowledge, EXCLUDED_CONFIG, "alex", include_contact=True)
        assert expected is not None

        raw = person_read(
            knowledge,
            "alex",
            include_contact_data=True,
            caller_audience=None,
            config=EXCLUDED_CONFIG,
        )

        assert json.loads(raw) == expected.to_dict()

    def test_restricted_caller_fails_closed_on_untagged_page(self, tmp_path: Path) -> None:
        from athenaeum.mcp_server import person_read

        # Page carries no `access:`/`audience:` grant, so it is withheld from
        # ANY restricted caller (fail-closed, mirrors `is_page_authorized`).
        knowledge = self._build_knowledge(tmp_path)

        raw = person_read(
            knowledge,
            "alex",
            include_contact_data=True,
            caller_audience=self.RESTRICTED,
            config=EXCLUDED_CONFIG,
        )
        payload = json.loads(raw)

        assert payload["ok"] is False
        assert payload.get("error_code") == "forbidden"
        assert "contact" not in payload
        assert "alex@example.org" not in raw

    def test_restricted_caller_authorized_page_succeeds(self, tmp_path: Path) -> None:
        from athenaeum.mcp_server import person_read

        knowledge = self._build_knowledge(
            tmp_path, page_extra="audience: [secondary]\n"
        )

        raw = person_read(
            knowledge,
            "alex",
            include_contact_data=True,
            caller_audience=self.RESTRICTED,
            config=EXCLUDED_CONFIG,
        )
        payload = json.loads(raw)

        assert "ok" not in payload  # a real PersonRead.to_dict(), not a refusal
        assert payload["contact"] == {"emails": ["alex@example.org"]}

    def test_unknown_uid_returns_json_error(self, tmp_path: Path) -> None:
        from athenaeum.mcp_server import person_read

        knowledge = tmp_path / "knowledge"
        (knowledge / "wiki").mkdir(parents=True)

        raw = person_read(knowledge, "ghost", caller_audience=None, config=EXCLUDED_CONFIG)
        payload = json.loads(raw)

        assert payload["ok"] is False
        assert "ghost" in payload["error"]
