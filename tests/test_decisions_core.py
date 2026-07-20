"""Unit tests for the `athenaeum.decisions` core helpers (issue #401)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from athenaeum.decisions import (
    _fallback_title,
    _first_body_line,
    _one_line,
    age_days,
    list_pending_decisions,
    source_info,
)


def test_age_days_basic() -> None:
    assert age_days("2026-06-20", today=date(2026, 7, 20)) == 30


def test_age_days_datetime_form() -> None:
    assert age_days("2026-07-01T09:15:00Z", today=date(2026, 7, 20)) == 19


def test_age_days_unparseable() -> None:
    assert age_days("not-a-date", today=date(2026, 7, 20)) is None
    assert age_days("", today=date(2026, 7, 20)) is None


def test_fallback_title_strips_uid_prefix() -> None:
    assert _fallback_title("/k/wiki/34f82884-auth-authentication.md") == "auth-authentication"


def test_fallback_title_strips_memory_prefix() -> None:
    assert _fallback_title("/k/user/user_alice_a.md") == "alice_a"


def test_fallback_title_plain() -> None:
    assert _fallback_title("/k/wiki/plainname.md") == "plainname"


def test_one_line_truncates() -> None:
    out = _one_line("a " * 200, limit=20)
    assert len(out) == 20
    assert out.endswith("…")


def test_first_body_line_skips_headings() -> None:
    assert _first_body_line("# Title\n\n## Sub\nReal content here.") == "Real content here."


def test_source_info_prefers_name_and_description(tmp_path: Path) -> None:
    page = tmp_path / "abc12345-jane.md"
    page.write_text(
        "---\nname: Jane Doe\ndescription: CEO of Acme.\n---\nbody line.\n",
        encoding="utf-8",
    )
    info = source_info(str(page))
    assert info == {"path": str(page), "title": "Jane Doe", "gist": "CEO of Acme."}


def test_source_info_gist_falls_back_to_body(tmp_path: Path) -> None:
    page = tmp_path / "abc12345-jane.md"
    page.write_text("---\nname: Jane Doe\n---\nFounder and CEO.\n", encoding="utf-8")
    info = source_info(str(page))
    assert info["title"] == "Jane Doe"
    assert info["gist"] == "Founder and CEO."


def test_source_info_missing_file(tmp_path: Path) -> None:
    info = source_info(str(tmp_path / "aa11bb22-lean-startup.md"))
    assert info["title"] == "lean-startup"
    assert info["gist"] == ""


def test_list_pending_decisions_empty(tmp_path: Path) -> None:
    (tmp_path / "wiki").mkdir()
    assert list_pending_decisions(tmp_path / "wiki") == []
