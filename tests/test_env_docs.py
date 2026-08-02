# SPDX-License-Identifier: Apache-2.0
"""CI gate + unit tests for the env-var documentation check (issue #688).

`test_no_undocumented_env_vars` is the enforcement gate — it fails the suite if
any `ATHENAEUM_*` name read by `src/` is missing from `docs/configuration.md`.
The rest prove the check itself works (that it detects an undocumented var, and
fails loudly on an empty scan side) so a green gate is trustworthy.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_env_docs.py"

_spec = importlib.util.spec_from_file_location("check_env_docs", _SCRIPT)
assert _spec and _spec.loader
check_env_docs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_env_docs)


def test_no_undocumented_env_vars() -> None:
    # The CI gate: every ATHENAEUM_* read by src/ must be documented.
    src_vars = check_env_docs.scan_tree(check_env_docs.SRC_DIR)
    doc_vars = check_env_docs.scan_docs(check_env_docs.CONFIG_DOC)
    missing = check_env_docs.undocumented(src_vars, doc_vars)
    assert not missing, (
        "undocumented ATHENAEUM_* vars read by src/ — document them in "
        f"docs/configuration.md (or allowlist with a reason): {sorted(missing)}"
    )


def test_main_returns_zero_on_current_tree(capsys: pytest.CaptureFixture[str]) -> None:
    assert check_env_docs.main([]) == 0
    out = capsys.readouterr().out
    assert "OK" in out
    # Reports a denominator even on success — evidence a sweep ran (AC).
    assert "documented of" in out


def test_detects_an_injected_undocumented_var() -> None:
    # AC plan step 4: an undocumented ATHENAEUM_* read must make the check red.
    src_vars = {"ATHENAEUM_CLASSIFY_MODEL", "ATHENAEUM_TOTALLY_NEW_KNOB"}
    doc_vars = {"ATHENAEUM_CLASSIFY_MODEL"}
    assert check_env_docs.undocumented(src_vars, doc_vars) == {
        "ATHENAEUM_TOTALLY_NEW_KNOB"
    }


def test_scan_is_digit_aware() -> None:
    # The reasoning-tier vars carry a digit; a [A-Z_]+ scan would truncate them.
    got = check_env_docs._scan("reads ATHENAEUM_REASONING_T1_MAX_TOKENS here")
    assert got == {"ATHENAEUM_REASONING_T1_MAX_TOKENS"}


def test_empty_src_side_fails_loudly(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # An empty scan side must fail loudly (exit 2), never pass green.
    empty = tmp_path / "empty_src"
    empty.mkdir()
    monkeypatch.setattr(check_env_docs, "SRC_DIR", empty)
    assert check_env_docs.main([]) == 2
    assert "no ATHENAEUM_* vars found" in capsys.readouterr().err


def test_empty_docs_side_fails_loudly(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    empty_doc = tmp_path / "empty.md"
    empty_doc.write_text("no env vars here\n", encoding="utf-8")
    monkeypatch.setattr(check_env_docs, "CONFIG_DOC", empty_doc)
    assert check_env_docs.main([]) == 2
    assert "docs scan is broken" in capsys.readouterr().err


def test_allowlisted_var_is_not_flagged(monkeypatch: pytest.MonkeyPatch) -> None:
    # An allowlisted var read by src/ counts as documented, not missing.
    monkeypatch.setattr(
        check_env_docs, "ALLOWLIST", {"ATHENAEUM_INTERNAL_X": "internal test hook"}
    )
    assert check_env_docs.undocumented({"ATHENAEUM_INTERNAL_X"}, set()) == set()
