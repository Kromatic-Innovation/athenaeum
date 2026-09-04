# SPDX-License-Identifier: Apache-2.0
"""CI gate + unit tests for the env-var documentation check (issues athenaeum#688,
athenaeum#1376).

`test_no_undocumented_env_vars` and `test_no_undocumented_by_code_env_vars` are
the two enforcement gates — the first fails the suite if any `ATHENAEUM_*` name
read by `src/` is missing from `docs/configuration.md` (src -> docs, athenaeum#688);
the second fails it if any name documented there is read by nothing the checker
can see (docs -> code, athenaeum#1376) — a documented-but-dead knob is silent
operator-facing rot the first direction cannot catch. The rest prove the check
itself works (detects an injected gap in each direction, recognises a
runtime-constructed family member as read, and fails loudly on every empty
scan side) so a green gate is trustworthy.
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


# --- docs -> code direction (issue athenaeum#1376) -----------------------------


def test_no_undocumented_by_code_env_vars() -> None:
    # The second CI gate: every ATHENAEUM_* documented in docs/configuration.md
    # must be read by src/, scripts/, examples/claude-code/, or a known
    # runtime-constructed family. A documented var nothing reads is an
    # operator-facing no-op with no error and no effect (issue athenaeum#1376).
    doc_vars = check_env_docs.scan_docs(check_env_docs.CONFIG_DOC)
    src_vars = check_env_docs.scan_tree(check_env_docs.SRC_DIR)
    scripts_vars = check_env_docs.scan_scripts(check_env_docs.SCRIPTS_DIR)
    hooks_vars = check_env_docs.scan_hooks(check_env_docs.HOOKS_DIR)
    families = check_env_docs.expand_families(check_env_docs.derive_knobs())
    read_vars = src_vars | scripts_vars | hooks_vars | families
    missing = check_env_docs.unread(doc_vars, read_vars)
    assert not missing, (
        "documented ATHENAEUM_* vars read by nothing — add a reader, remove the "
        f"doc entry, or allowlist with a reason in DOCS_TO_CODE_ALLOWLIST: {sorted(missing)}"
    )


def test_main_docs_to_code_denominator_on_current_tree(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Both directions print a denominator on success (AC6), not just the
    # original src -> docs direction.
    assert check_env_docs.main([]) == 0
    out = capsys.readouterr().out
    assert "OK (docs->code)" in out
    assert "read of" in out and "documented in" in out


def test_documented_but_unread_name_is_reported() -> None:
    # A name that is documented but that no scanned surface or family reads
    # is exactly the class this direction exists to catch (AC4).
    doc_vars = {"ATHENAEUM_GHOST_KNOB"}
    read_vars: set[str] = set()
    assert check_env_docs.unread(doc_vars, read_vars) == {"ATHENAEUM_GHOST_KNOB"}


def test_runtime_constructed_family_member_is_not_reported() -> None:
    # athenaeum.provider.resolve_provider builds
    # f"ATHENAEUM_{knob.upper()}_LLM_PROVIDER" at runtime, so no literal token
    # for e.g. ATHENAEUM_WRITE_LLM_PROVIDER exists in src/ — the family
    # expansion, not a literal scan, is what makes it count as read (AC2/AC3).
    families = check_env_docs.expand_families({"write"})
    assert families == {"ATHENAEUM_WRITE_LLM_PROVIDER", "ATHENAEUM_WRITE_THINKING"}
    doc_vars = {"ATHENAEUM_WRITE_LLM_PROVIDER"}
    assert check_env_docs.unread(doc_vars, families) == set()


def test_hook_only_variable_is_not_reported() -> None:
    # ATHENAEUM_CLI and its siblings are read only by the shipped
    # examples/claude-code/*.sh hooks, outside the original src/-only scan
    # (issue athenaeum#1376 class a). scan_hooks is what makes them visible.
    got = check_env_docs.scan_hooks(check_env_docs.HOOKS_DIR)
    assert "ATHENAEUM_CLI" in got
    assert check_env_docs.unread({"ATHENAEUM_CLI"}, got) == set()


def test_derive_knobs_matches_prompt_registry() -> None:
    # The knob set must come from src/ (AC2), not a hardcoded list here — this
    # pins that derive_knobs() really does read the live registry rather than
    # a copy that could drift from it.
    from athenaeum import prompt_registry

    knobs = check_env_docs.derive_knobs()
    assert knobs == set(prompt_registry.KNOBS)
    assert "write" in knobs  # covers ATHENAEUM_WRITE_THINKING with no allowlist entry


def test_stale_docs_to_code_allowlist_now_read(monkeypatch: pytest.MonkeyPatch) -> None:
    # An allowlist entry for a name that is now read must be surfaced, not
    # silently kept forever (AC7).
    monkeypatch.setattr(
        check_env_docs,
        "DOCS_TO_CODE_ALLOWLIST",
        {"ATHENAEUM_NOW_READ": "was unread at allowlist time"},
    )
    stale = check_env_docs.stale_docs_to_code_allowlist(
        doc_vars={"ATHENAEUM_NOW_READ"}, read_vars={"ATHENAEUM_NOW_READ"}
    )
    assert stale == {"ATHENAEUM_NOW_READ"}


def test_stale_docs_to_code_allowlist_no_longer_documented(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        check_env_docs,
        "DOCS_TO_CODE_ALLOWLIST",
        {"ATHENAEUM_REMOVED_FROM_DOCS": "was documented-but-unread at allowlist time"},
    )
    stale = check_env_docs.stale_docs_to_code_allowlist(doc_vars=set(), read_vars=set())
    assert stale == {"ATHENAEUM_REMOVED_FROM_DOCS"}


def test_empty_scripts_side_fails_loudly(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # AC1: the widened scripts/ scan surface fails loudly if empty too.
    empty = tmp_path / "empty_scripts"
    empty.mkdir()
    monkeypatch.setattr(check_env_docs, "SCRIPTS_DIR", empty)
    assert check_env_docs.main([]) == 2
    assert "scripts scan is broken" in capsys.readouterr().err


def test_empty_hooks_side_fails_loudly(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # AC1: the widened examples/claude-code/ scan surface fails loudly if empty too.
    empty = tmp_path / "empty_hooks"
    empty.mkdir()
    monkeypatch.setattr(check_env_docs, "HOOKS_DIR", empty)
    assert check_env_docs.main([]) == 2
    assert "hooks scan is broken" in capsys.readouterr().err


def test_empty_derived_knob_set_fails_loudly(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # AC2/AC3's family expansion is only as good as its knob source; a knob
    # derivation that silently came back empty must not report a false pass
    # (the same "empty side" trap the rest of this module guards against).
    monkeypatch.setattr(check_env_docs, "derive_knobs", lambda: set())
    assert check_env_docs.main([]) == 2
    assert "KNOBS came back empty" in capsys.readouterr().err


def test_docs_to_code_failure_uses_distinct_exit_code(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # AC5: the docs -> code failure must exit with a code distinct from both
    # the src -> docs failure (1) and the empty-scan error (2), and its
    # message must name the direction. The doc fixture is the REAL
    # configuration.md plus one extra ghost name, so direction 1 (src -> docs)
    # still passes clean and only direction 2 (docs -> code) trips.
    real_doc_text = check_env_docs.CONFIG_DOC.read_text(encoding="utf-8")
    doc = tmp_path / "configuration.md"
    doc.write_text(real_doc_text + "\nATHENAEUM_GHOST_KNOB is documented here.\n", encoding="utf-8")
    monkeypatch.setattr(check_env_docs, "CONFIG_DOC", doc)
    assert check_env_docs.main([]) == 3
    err = capsys.readouterr().err
    assert "docs->code" in err
    assert "ATHENAEUM_GHOST_KNOB" in err
