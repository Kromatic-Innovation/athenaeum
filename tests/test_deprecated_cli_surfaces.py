# SPDX-License-Identifier: Apache-2.0
"""Tests for the deprecation-flag mechanism (issue athenaeum#1992 AC1/AC2/AC5).

Covers:
- :func:`athenaeum.config.resolve_deprecated_cli_surfaces_enabled` and
  :func:`athenaeum.config.deprecated_cli_surface_message` (the mechanism
  itself, env/yaml precedence, default state).
- ``athenaeum merges`` / ``athenaeum questions`` actually print the
  deprecation message on every invocation, pointing at ``athenaeum
  decisions`` (AC2), while the surface itself keeps working unchanged
  (AC5 — reachable, just labeled; no half-cut-over block).
- ``athenaeum decisions migrate`` is wired end-to-end through the real
  CLI parser.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from athenaeum.config import (
    DEPRECATED_CLI_SURFACE_MESSAGES,
    DEPRECATED_CLI_SURFACE_REMOVAL_VERSION,
    deprecated_cli_surface_message,
    resolve_deprecated_cli_surfaces_enabled,
)


def test_enabled_by_default_with_no_config() -> None:
    assert resolve_deprecated_cli_surfaces_enabled(None) is True
    assert resolve_deprecated_cli_surfaces_enabled({}) is True


def test_env_override_disables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATHENAEUM_DEPRECATED_CLI_SURFACES_ENABLED", "0")
    assert resolve_deprecated_cli_surfaces_enabled(None) is False


def test_env_override_truthy_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATHENAEUM_DEPRECATED_CLI_SURFACES_ENABLED", "yes")
    assert resolve_deprecated_cli_surfaces_enabled(None) is True


def test_yaml_override_disables() -> None:
    config = {"librarian": {"deprecated_cli_surfaces_enabled": False}}
    assert resolve_deprecated_cli_surfaces_enabled(config) is False


def test_env_wins_over_yaml(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATHENAEUM_DEPRECATED_CLI_SURFACES_ENABLED", "1")
    config = {"librarian": {"deprecated_cli_surfaces_enabled": False}}
    assert resolve_deprecated_cli_surfaces_enabled(config) is True


def test_message_present_for_declared_surfaces() -> None:
    for surface in ("merges", "questions"):
        message = deprecated_cli_surface_message(surface, None)
        assert message
        assert "athenaeum decisions" in message


def test_message_empty_for_unknown_surface() -> None:
    assert deprecated_cli_surface_message("not-a-real-surface", None) == ""


def test_message_empty_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATHENAEUM_DEPRECATED_CLI_SURFACES_ENABLED", "0")
    assert deprecated_cli_surface_message("merges", None) == ""


def test_every_declared_surface_has_a_nonempty_message() -> None:
    assert set(DEPRECATED_CLI_SURFACE_MESSAGES) == {"merges", "questions"}
    for message in DEPRECATED_CLI_SURFACE_MESSAGES.values():
        assert message and "deprecated" in message.lower()


def test_every_declared_surface_names_its_removal_version() -> None:
    """Issue athenaeum#2138: an open-ended deprecation tells a reader nothing.

    The panel found the stderr DEPRECATED message correct but with no target
    version, so a consumer could not tell whether to migrate this week or next
    year. Every declared message must name
    :data:`DEPRECATED_CLI_SURFACE_REMOVAL_VERSION`.
    """
    for surface, message in DEPRECATED_CLI_SURFACE_MESSAGES.items():
        assert DEPRECATED_CLI_SURFACE_REMOVAL_VERSION in message, surface


def test_removal_version_is_a_minor_not_a_patch() -> None:
    """Removing public surface bumps MINOR pre-1.0, never PATCH.

    PATCH is a per-merge build counter here (CONTRIBUTING, "Versioning and
    releases"), so a removal target written as a PATCH would be meaningless.
    """
    parts = DEPRECATED_CLI_SURFACE_REMOVAL_VERSION.split(".")
    assert len(parts) == 3, DEPRECATED_CLI_SURFACE_REMOVAL_VERSION
    assert all(part.isdigit() for part in parts), DEPRECATED_CLI_SURFACE_REMOVAL_VERSION
    assert parts[2] == "0", DEPRECATED_CLI_SURFACE_REMOVAL_VERSION


def test_removal_version_reaches_the_user_facing_warning(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """The version has to be in the stderr line, not merely in the constant."""
    from athenaeum._cmd_merges import cmd_merges

    args = argparse.Namespace(merges_target="count", path=tmp_path, json=True)
    assert cmd_merges(args) == 0
    assert DEPRECATED_CLI_SURFACE_REMOVAL_VERSION in capsys.readouterr().err


def test_cmd_merges_warns_and_still_works(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    from athenaeum._cmd_merges import cmd_merges

    args = argparse.Namespace(merges_target="count", path=tmp_path, json=True)
    rc = cmd_merges(args)
    captured = capsys.readouterr()

    assert rc == 0
    assert "DEPRECATED" in captured.err
    assert "athenaeum decisions" in captured.err
    # The surface itself still works unchanged (AC5: reachable, not blocked).
    assert json.loads(captured.out) == {"count": 0, "oldest": None}


def test_cmd_questions_warns_and_still_works(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    from athenaeum._cmd_questions import cmd_questions

    args = argparse.Namespace(questions_target="count", path=tmp_path, json=True)
    rc = cmd_questions(args)
    captured = capsys.readouterr()

    assert rc == 0
    assert "DEPRECATED" in captured.err
    assert "athenaeum decisions" in captured.err
    assert json.loads(captured.out) == {"count": 0, "oldest": None}


def test_cmd_merges_warning_silenceable_via_env(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    from athenaeum._cmd_merges import cmd_merges

    monkeypatch.setenv("ATHENAEUM_DEPRECATED_CLI_SURFACES_ENABLED", "0")
    args = argparse.Namespace(merges_target="count", path=tmp_path, json=True)
    rc = cmd_merges(args)
    captured = capsys.readouterr()

    assert rc == 0
    assert "DEPRECATED" not in captured.err


def test_decisions_migrate_wired_through_real_cli(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    from athenaeum.cli import main

    knowledge_root = tmp_path / "knowledge"
    (knowledge_root / "wiki").mkdir(parents=True)

    rc = main(["decisions", "migrate", "--path", str(knowledge_root), "--json"])
    captured = capsys.readouterr()

    assert rc == 0
    payload = json.loads(captured.out)
    assert payload["merge_count"] == 0
    assert payload["question_count"] == 0
    assert payload["total"] == 0
    assert (knowledge_root / "wiki" / "_decisions_queue.jsonl").exists()
