# SPDX-License-Identifier: Apache-2.0
"""Issue athenaeum#2766 -- the plan-usage disclosure carries magnitude, and the
README compile step matches the provider ``init`` printed."""

from __future__ import annotations

from pathlib import Path

import pytest

from athenaeum.cli import main as cli_main
from athenaeum.librarian import (
    DEFAULT_MAX_API_CALLS,
    DEFAULT_MAX_FILES,
    SESSION_END_MAX_API_CALLS,
    SESSION_END_MAX_FILES,
)

_ROOT = Path(__file__).resolve().parent.parent


def test_init_disclosure_states_measured_figures_with_date_and_caps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ATHENAEUM_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("ATHENAEUM_TOPIC_LLM_PROVIDER", raising=False)
    import athenaeum.provider as provider_mod

    monkeypatch.setattr(provider_mod.shutil, "which", lambda _b: "/usr/bin/claude")
    # Pretend a login exists where the login probe is present (athenaeum#2762).
    monkeypatch.setattr("athenaeum.doctor._claude_login_missing", lambda: False, raising=False)

    assert cli_main(["init", "--path", str(tmp_path / "knowledge")]) == 0
    out = capsys.readouterr().out
    assert "usage limits" in out
    assert "0.28.0 (2026-11-20) and 0.29.0 (2026-11-21)" in out
    assert "3,200-7,000 tokens" in out
    assert "18-50 s" in out
    assert "2-3 model calls" in out
    assert f"{DEFAULT_MAX_FILES} files / {DEFAULT_MAX_API_CALLS} API calls" in out
    assert f"{SESSION_END_MAX_FILES} files / {SESSION_END_MAX_API_CALLS} API calls" in out


def test_install_guide_documents_figures_and_caps() -> None:
    text = " ".join((_ROOT / "docs/guides/install.md").read_text(encoding="utf-8").split())
    assert "0.28.0 (2026-11-20) and 0.29.0 (2026-11-21)" in text
    assert "3,200 to 7,000 tokens" in text
    assert "9,233 tokens" in text
    assert f"{DEFAULT_MAX_FILES} files or {DEFAULT_MAX_API_CALLS} API calls" in text
    assert f"{SESSION_END_MAX_FILES} files or {SESSION_END_MAX_API_CALLS} API calls" in text


def test_readme_compile_step_matches_init_provider_choice() -> None:
    readme = (_ROOT / "README.md").read_text(encoding="utf-8")
    assert "# or configure the claude-cli backend" not in readme
    text = " ".join(readme.split())
    assert "`athenaeum init` already chose the compile provider" in text
    assert "selected `claude-cli`" in text
    assert "3,200 to 7,000 tokens and 18 to 50 s per note" in text
    assert "An Anthropic API key is the alternative" in text
