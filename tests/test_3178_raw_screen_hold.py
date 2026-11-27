# SPDX-License-Identifier: Apache-2.0
"""Screen raw intake first in a run; hold unscreened raw files out of commits (athenaeum#3178).

All values are synthetic markers.
"""

from __future__ import annotations

import argparse
import logging
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from athenaeum import raw_screening, sensitivity
from athenaeum.config import StorageConfigError, load_config, resolve_hold_unscreened_raw
from athenaeum.init import init_knowledge_dir
from athenaeum.storage import open_store

ROUTED = "zz-routed-3178@example.invalid"
DISCARDED = "zz-discarded-3178-token"
FIELD_ROUTED = "zz-field-routed-3178"
RAW_REL = "raw/src/20260101T000000-aaaaaaaa.md"
_NOT_CORPUS = {"embedded": False, "recallable": False, "merge_eligible": False}


class _Crash(BaseException):
    """Stands in for a process killed at an exact point (not an Exception)."""


class _TokenRecognizer:
    name = "tokenish-3178"

    def detect(self, *, text: str, frontmatter: Any) -> list[Any]:
        start = text.find(DISCARDED)
        if start == -1:
            return []
        span = (start, start + len(DISCARDED))
        return [sensitivity.SensitivityMatch(recognizer=self.name, value=DISCARDED, span=span)]


def _cfg(vault: Path, *, hold: bool | None = None, extra_routes: list[dict] | None = None) -> dict:
    cfg: dict[str, Any] = {
        "sensitivity": {
            "routing": {"enabled": True},
            "classes": {
                "tokenish": {
                    "recognizers": ["tokenish-3178"],
                    "read_policy": {"access": "confidential"},
                },
            },
        },
        "storage": {
            "adapters": {
                "vault": {
                    "backing_store": "markdown",
                    "surface_root": str(vault),
                    "corpus_policy": _NOT_CORPUS,
                    "sensitive": True,
                }
            },
            "routes": [
                {"sensitivity_class": "pii", "to": ["vault"]},
                {"sensitivity_class": "tokenish", "to": []},
                *(extra_routes or []),
            ],
        },
    }
    if hold is not None:
        cfg["librarian"] = {"hold_unscreened_raw": hold}
    return cfg


@pytest.fixture(autouse=True)
def _env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        sensitivity, "_REGISTERED_RECOGNIZERS", dict(sensitivity._REGISTERED_RECOGNIZERS)
    )
    sensitivity.register_recognizer(_TokenRecognizer(), replace=True)
    monkeypatch.setenv("ATHENAEUM_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    # No model is called: the provider is a stub binary that always fails, so the
    # same thing happens on a host with and without the claude CLI installed.
    stub = tmp_path / "bin" / "claude-stub"
    stub.parent.mkdir()
    stub.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    stub.chmod(0o755)
    monkeypatch.setenv("ATHENAEUM_LLM_PROVIDER", "claude-cli")
    monkeypatch.setenv("ATHENAEUM_CLAUDE_CLI_BIN", str(stub))
    monkeypatch.setattr(raw_screening, "_EMITTED", set())
    # The hold is registered per knowledge root, process-wide: drop what a test leaves.
    from athenaeum import store

    monkeypatch.setattr(store, "_RAW_HOLDS", {})


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout


def _kb(tmp_path: Path, cfg: dict | None) -> Path:
    kb = init_knowledge_dir(tmp_path / "kb")
    path = kb / "athenaeum.yaml"
    merged = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    for key, value in (cfg or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key].update(value)
        else:
            merged[key] = value
    path.write_text(yaml.safe_dump(merged), encoding="utf-8")
    _git(kb, "add", "-A")
    _git(kb, "commit", "-qm", "config")
    return kb


def _put_raw(kb: Path, text: str, name: str = "20260101T000000-aaaaaaaa.md") -> Path:
    path = kb / "raw" / "src" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _committed(kb: Path, rel: str) -> bool:
    return subprocess.run(
        ["git", "-C", str(kb), "cat-file", "-e", f"HEAD:{rel}"], capture_output=True
    ).returncode == 0


def _all_history(repo: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "log", "--all", "-p", "--format=%H"],
        capture_output=True,
        text=True,
    ).stdout


def _vault_records(vault: Path) -> list[Path]:
    return sorted(vault.rglob("*.md")) if vault.exists() else []


# ---------------------------------------------------------------------------
# The setting
# ---------------------------------------------------------------------------


def test_setting_defaults_on_and_is_a_boolean() -> None:
    assert resolve_hold_unscreened_raw(None) is True
    assert resolve_hold_unscreened_raw({}) is True
    assert resolve_hold_unscreened_raw({"librarian": {"hold_unscreened_raw": False}}) is False
    assert resolve_hold_unscreened_raw({"librarian": {"hold_unscreened_raw": True}}) is True
    for bad in ("false", "yes", 0, 1, None, []):
        with pytest.raises(StorageConfigError):
            resolve_hold_unscreened_raw({"librarian": {"hold_unscreened_raw": bad}})


def test_a_non_boolean_setting_fails_the_run(tmp_path: Path) -> None:
    from athenaeum.librarian import run

    cfg = _cfg(tmp_path / "vault")
    cfg["librarian"] = {"hold_unscreened_raw": "no"}
    kb = _kb(tmp_path, cfg)
    assert run(raw_root=kb / "raw", wiki_root=kb / "wiki", knowledge_root=kb, merge_only=True) == 1


def test_hold_takes_effect_only_with_a_rule() -> None:
    assert raw_screening.hold_active({}) is False
    assert raw_screening.hold_active({"librarian": {"hold_unscreened_raw": True}}) is False
    assert raw_screening.hold_active({"sensitivity": {"routing": {"enabled": True}}}) is True
    only_route = {"storage": {"routes": []}}
    assert raw_screening.rules_configured(only_route) is False
    assert raw_screening.hold_active(_cfg(Path("v"))) is True
    assert raw_screening.hold_active(_cfg(Path("v"), hold=False)) is False


# ---------------------------------------------------------------------------
# The screening phase
# ---------------------------------------------------------------------------


def test_screening_replaces_the_value_and_writes_the_vault_first(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    cfg = _cfg(vault)
    kb = _kb(tmp_path, cfg)
    raw = _put_raw(kb, f"Mail {ROUTED} and keep {DISCARDED} secret.\n")
    report = raw_screening.screen_raw_intake(kb, cfg)
    assert report.active and report.screened == 1 and len(report.rewritten) == 1
    text = raw.read_text(encoding="utf-8")
    assert ROUTED not in text and DISCARDED not in text
    assert "[sensitive:pii:" in text and "value not stored" in text
    records = _vault_records(vault)
    assert len(records) == 1 and ROUTED in records[0].read_text(encoding="utf-8")


def test_declared_fields_are_screened(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    cfg = _cfg(
        vault,
        extra_routes=[{"page_type": "reference", "fields": ["routed_field"], "to": ["vault"]}],
    )
    kb = _kb(tmp_path, cfg)
    raw = _put_raw(
        kb, f"---\ntype: reference\nname: Ref\nrouted_field: {FIELD_ROUTED}\n---\n\nBody.\n"
    )
    raw_screening.screen_raw_intake(kb, cfg)
    assert FIELD_ROUTED not in raw.read_text(encoding="utf-8")
    assert [FIELD_ROUTED in p.read_text(encoding="utf-8") for p in _vault_records(vault)] == [True]


def _snapshot_bytes(a: Path, b: Path, vault: Path) -> tuple[bytes, bytes, list[bytes]]:
    return (a.read_bytes(), b.read_bytes(), [p.read_bytes() for p in _vault_records(vault)])


def test_screening_is_idempotent_two_runs_identical_bytes(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    cfg = _cfg(
        vault,
        extra_routes=[{"page_type": "reference", "fields": ["routed_field"], "to": ["vault"]}],
    )
    kb = _kb(tmp_path, cfg)
    a = _put_raw(kb, f"Mail {ROUTED}.\n", "20260101T000000-aaaaaaaa.md")
    b = _put_raw(
        kb,
        f"---\ntype: reference\nname: R\nrouted_field: {FIELD_ROUTED}\n---\n\nMail {ROUTED}.\n",
        "20260101T000001-bbbbbbbb.md",
    )
    raw_screening.screen_raw_intake(kb, cfg)
    first = _snapshot_bytes(a, b, vault)
    second_report = raw_screening.screen_raw_intake(kb, cfg)
    assert second_report.rewritten == [] and second_report.already == 2
    assert _snapshot_bytes(a, b, vault) == first
    # With the ledger gone the screen itself is still a no-op on screened text.
    shutil.rmtree(tmp_path / "cache")
    third_report = raw_screening.screen_raw_intake(kb, cfg)
    assert third_report.rewritten == [] and third_report.screened == 2
    assert _snapshot_bytes(a, b, vault) == first


def test_no_rule_the_phase_does_nothing(tmp_path: Path) -> None:
    kb = _kb(tmp_path, None)
    raw = _put_raw(kb, f"Mail {ROUTED}.\n")
    before = raw.read_bytes()
    report = raw_screening.screen_raw_intake(kb, {})
    assert report.active is False and report.scanned == 0
    assert raw.read_bytes() == before
    assert not (tmp_path / "cache" / raw_screening.LEDGER_DIRNAME).exists()


# ---------------------------------------------------------------------------
# The hold
# ---------------------------------------------------------------------------


def test_snapshot_leaves_an_unscreened_raw_file_out_and_commits_the_rest(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path / "vault")
    kb = _kb(tmp_path, cfg)
    _put_raw(kb, f"Mail {ROUTED}.\n")
    (kb / "wiki" / "note.md").write_text("plain page\n", encoding="utf-8")
    sha = open_store(cfg, kb).snapshot("snap")
    assert sha is not None
    assert _committed(kb, "wiki/note.md") and not _committed(kb, RAW_REL)
    assert ROUTED not in _all_history(kb)
    assert RAW_REL in _git(kb, "status", "--porcelain", "-uall")


def test_a_snapshot_with_only_held_changes_makes_no_commit(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path / "vault")
    kb = _kb(tmp_path, cfg)
    _put_raw(kb, f"Mail {ROUTED}.\n")
    head = _git(kb, "rev-parse", "HEAD")
    assert open_store(cfg, kb).snapshot("snap") is None
    assert _git(kb, "rev-parse", "HEAD") == head


def test_after_screening_the_snapshot_commits_the_screened_text(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path / "vault")
    kb = _kb(tmp_path, cfg)
    raw = _put_raw(kb, f"Mail {ROUTED}.\n")
    raw_screening.screen_raw_intake(kb, cfg)
    assert open_store(cfg, kb).snapshot("snap") is not None
    assert _git(kb, "show", f"HEAD:{RAW_REL}") == raw.read_text(encoding="utf-8")
    assert ROUTED not in _all_history(kb)


def test_a_file_rewritten_by_another_program_after_screening_is_held_again(
    tmp_path: Path,
) -> None:
    cfg = _cfg(tmp_path / "vault")
    kb = _kb(tmp_path, cfg)
    raw = _put_raw(kb, "nothing sensitive\n")
    raw_screening.screen_raw_intake(kb, cfg)
    open_store(cfg, kb).snapshot("snap")
    raw.write_text(f"now with {ROUTED}\n", encoding="utf-8")
    assert open_store(cfg, kb).snapshot("snap again") is None
    assert ROUTED not in _all_history(kb)


def test_a_file_staged_by_something_else_is_taken_back_out(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path / "vault")
    kb = _kb(tmp_path, cfg)
    _put_raw(kb, f"Mail {ROUTED}.\n")
    (kb / "wiki" / "note.md").write_text("plain\n", encoding="utf-8")
    _git(kb, "add", "-A")
    assert open_store(cfg, kb).snapshot("snap") is not None
    assert not _committed(kb, RAW_REL) and ROUTED not in _all_history(kb)


def test_a_path_with_glob_characters_is_matched_literally(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path / "vault")
    kb = _kb(tmp_path, cfg)
    _put_raw(kb, f"Mail {ROUTED}.\n", "a[1]*.md")
    _put_raw(kb, "plain\n", "b.md")
    raw_screening.screen_raw_intake(kb, cfg)
    _put_raw(kb, f"Mail {ROUTED}.\n", "a[2]*.md")
    open_store(cfg, kb).snapshot("snap")
    assert _committed(kb, "raw/src/a[1]*.md") and _committed(kb, "raw/src/b.md")
    assert not _committed(kb, "raw/src/a[2]*.md")


def test_the_retirement_snapshot_commits_screened_text_and_skips_unscreened(
    tmp_path: Path,
) -> None:
    from athenaeum.retire import _commit_paths_if_staged

    cfg = _cfg(tmp_path / "vault")
    kb = _kb(tmp_path, cfg)
    screened = _put_raw(kb, f"Mail {ROUTED}.\n", "20260101T000000-aaaaaaaa.md")
    raw_screening.screen_raw_intake(kb, cfg)
    unscreened = _put_raw(kb, f"Mail {ROUTED}.\n", "20260101T000001-bbbbbbbb.md")
    open_store(cfg, kb)  # registers the hold
    rels = [str(p.relative_to(kb)) for p in (screened, unscreened)]
    assert _commit_paths_if_staged(kb, rels, "provenance") is True
    assert _committed(kb, rels[0]) and not _committed(kb, rels[1])
    assert ROUTED not in _all_history(kb)
    assert _commit_paths_if_staged(kb, [rels[1]], "provenance") is False


def test_hold_off_stages_raw_files_as_today_and_names_them(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path / "vault", hold=False)
    kb = _kb(tmp_path, cfg)
    _put_raw(kb, f"Mail {ROUTED}.\n")
    assert open_store(cfg, kb).snapshot("snap") is not None
    assert _committed(kb, RAW_REL)


def test_unscreenable_files_are_named_with_the_hold_off_and_counted_with_it_on(
    tmp_path: Path,
) -> None:
    for hold in (False, True):
        sub = tmp_path / f"h{hold}"
        sub.mkdir()
        cfg = _cfg(
            sub / "vault",
            hold=hold,
            extra_routes=[{"page_type": "secret_page", "to": ["vault"]}],
        )
        kb = _kb(sub, cfg)
        _put_raw(kb, "no declared type here\n")
        report = raw_screening.screen_raw_intake(kb, cfg)
        assert report.needs_processing == [RAW_REL]
        fields = report.profile_fields()
        if hold:
            assert fields["held"] == 1
            assert open_store(cfg, kb).snapshot("snap") is None
            assert not _committed(kb, RAW_REL)
        else:
            assert RAW_REL in str(fields["unscreened_staged"])
            assert open_store(cfg, kb).snapshot("snap") is not None
            assert _committed(kb, RAW_REL)


def test_processing_releases_a_file_that_could_not_be_screened(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path / "vault", extra_routes=[{"page_type": "secret_page", "to": ["vault"]}])
    kb = _kb(tmp_path, cfg)
    raw = _put_raw(kb, "no declared type here\n")
    raw_screening.screen_raw_intake(kb, cfg)
    assert open_store(cfg, kb).snapshot("snap") is None
    assert raw_screening.mark_processed(kb, cfg, raw) is True
    assert open_store(cfg, kb).snapshot("snap") is not None
    assert _committed(kb, RAW_REL)


def test_a_failed_screen_is_not_released_by_processing(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.write_text("a file where the vault must go", encoding="utf-8")
    cfg = _cfg(vault, extra_routes=[{"page_type": "secret_page", "to": ["vault"]}])
    kb = _kb(tmp_path, cfg)
    raw = _put_raw(kb, f"---\ntype: note\n---\nMail {ROUTED}.\n")
    report = raw_screening.screen_raw_intake(kb, cfg)
    assert report.failed == [RAW_REL]
    assert raw_screening.mark_processed(kb, cfg, raw) is False
    assert open_store(cfg, kb).snapshot("snap") is None


def test_shared_store_intake_answers_and_operational_sources_are_not_held(
    tmp_path: Path,
) -> None:
    cfg = _cfg(tmp_path / "vault")
    cfg["librarian"] = {"non_intake_sources": ["toollogs"]}
    kb = _kb(tmp_path, cfg)
    for rel in ("raw/_stores/team/m.md", "raw/answers/a.md", "raw/toollogs/l.md"):
        p = kb / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"Mail {ROUTED}.\n", encoding="utf-8")
    report = raw_screening.screen_raw_intake(kb, cfg)
    assert report.scanned == 0
    assert open_store(cfg, kb).snapshot("snap") is not None
    for rel in ("raw/_stores/team/m.md", "raw/answers/a.md", "raw/toollogs/l.md"):
        assert _committed(kb, rel)


def test_a_symlinked_raw_path_is_neither_screened_nor_held(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path / "vault")
    kb = _kb(tmp_path, cfg)
    target = tmp_path / "elsewhere"
    target.mkdir()
    (target / "x.md").write_text(f"Mail {ROUTED}.\n", encoding="utf-8")
    (kb / "raw" / "linked").symlink_to(target, target_is_directory=True)
    assert raw_screening.screen_raw_intake(kb, cfg).scanned == 0
    assert open_store(cfg, kb).snapshot("snap") is not None
    assert _committed(kb, "raw/linked")


def test_history_none_still_rewrites_raw_files(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path / "vault")
    cfg["storage"]["history"] = "none"
    kb = _kb(tmp_path, cfg)
    raw = _put_raw(kb, f"Mail {ROUTED}.\n")
    raw_screening.screen_raw_intake(kb, cfg)
    assert ROUTED not in raw.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# No rule: nothing changes
# ---------------------------------------------------------------------------


def test_no_rule_every_commit_is_byte_identical_to_a_plain_add_all(tmp_path: Path) -> None:
    kb_a = _kb(tmp_path / "a", None) if (tmp_path / "a").mkdir() is None else None
    assert kb_a is not None
    _put_raw(kb_a, f"Mail {ROUTED}.\n")
    (kb_a / "wiki" / "note.md").write_text("plain\n", encoding="utf-8")
    kb_b = tmp_path / "b" / "kb"
    kb_b.parent.mkdir()
    shutil.copytree(kb_a, kb_b)
    open_store({}, kb_a).snapshot("snap")
    _git(kb_b, "add", "-A")
    _git(kb_b, "commit", "-qm", "snap")
    assert _git(kb_a, "rev-parse", "HEAD^{tree}") == _git(kb_b, "rev-parse", "HEAD^{tree}")
    assert _committed(kb_a, RAW_REL)


def test_hold_registered_only_with_a_rule(tmp_path: Path) -> None:
    from athenaeum import store

    kb = _kb(tmp_path, None)
    open_store({}, kb)
    assert store._RAW_HOLDS == {}
    open_store(_cfg(tmp_path / "vault"), kb)
    assert kb.resolve() in store._RAW_HOLDS
    open_store({}, kb)  # a config with no rule withdraws it
    assert store._RAW_HOLDS == {}


# ---------------------------------------------------------------------------
# Crash tests (hold on)
# ---------------------------------------------------------------------------


def test_crash_after_the_vault_write_before_the_raw_rewrite_converges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    cfg = _cfg(vault)
    kb = _kb(tmp_path, cfg)
    raw = _put_raw(kb, f"Mail {ROUTED}.\n")
    original = raw.read_bytes()

    def _boom(*_a: Any, **_k: Any) -> None:
        raise _Crash

    with monkeypatch.context() as m:
        m.setattr(raw_screening, "atomic_write_text", _boom)
        with pytest.raises(_Crash):
            raw_screening.screen_raw_intake(kb, cfg)
    assert raw.read_bytes() == original and len(_vault_records(vault)) == 1
    assert open_store(cfg, kb).snapshot("snap") is None  # still held
    report = raw_screening.screen_raw_intake(kb, cfg)
    assert report.screened == 1
    assert len(_vault_records(vault)) == 1  # converged on the same record
    assert ROUTED not in raw.read_text(encoding="utf-8")
    open_store(cfg, kb).snapshot("snap")
    assert ROUTED not in _all_history(kb) and _committed(kb, RAW_REL)


def test_crash_after_the_rewrite_before_any_commit_loses_nothing_commits_nothing_unscreened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    cfg = _cfg(vault)
    kb = _kb(tmp_path, cfg)
    raw = _put_raw(kb, f"Mail {ROUTED}.\n")

    def _boom(*_a: Any, **_k: Any) -> None:
        raise _Crash

    with monkeypatch.context() as m:
        m.setattr(raw_screening, "_save_ledger", _boom)
        with pytest.raises(_Crash):
            raw_screening.screen_raw_intake(kb, cfg)
    assert ROUTED not in raw.read_text(encoding="utf-8")  # the rewrite landed
    assert [ROUTED in p.read_text(encoding="utf-8") for p in _vault_records(vault)] == [True]
    # Not marked, so not trusted: nothing is committed from it.
    assert open_store(cfg, kb).snapshot("snap") is None
    assert not _committed(kb, RAW_REL) and ROUTED not in _all_history(kb)
    report = raw_screening.screen_raw_intake(kb, cfg)
    assert report.rewritten == [] and report.screened == 1
    assert len(_vault_records(vault)) == 1
    open_store(cfg, kb).snapshot("snap")
    assert _committed(kb, RAW_REL) and ROUTED not in _all_history(kb)


def test_a_failed_vault_write_leaves_the_file_untouched_unscreened_uncommitted_and_reported(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    vault.write_text("a file where the vault directory must go", encoding="utf-8")
    cfg = _cfg(vault)
    kb = _kb(tmp_path, cfg)
    raw = _put_raw(kb, f"Mail {ROUTED}.\n")
    original = raw.read_bytes()
    report = raw_screening.screen_raw_intake(kb, cfg)
    assert report.failed == [RAW_REL] and report.rewritten == []
    assert report.profile_fields()["failed"] == 1
    assert raw.read_bytes() == original
    assert open_store(cfg, kb).snapshot("snap") is None
    assert not _committed(kb, RAW_REL) and ROUTED not in _all_history(kb)


def test_one_of_several_vault_targets_failing_leaves_the_file_unscreened(tmp_path: Path) -> None:
    good, bad = tmp_path / "vault", tmp_path / "vault2"
    bad.write_text("blocker", encoding="utf-8")
    cfg = _cfg(good)
    cfg["storage"]["adapters"]["vault2"] = {
        "backing_store": "markdown",
        "surface_root": str(bad),
        "corpus_policy": _NOT_CORPUS,
        "sensitive": True,
    }
    cfg["storage"]["routes"][0] = {"sensitivity_class": "pii", "to": ["vault", "vault2"]}
    kb = _kb(tmp_path, cfg)
    raw = _put_raw(kb, f"Mail {ROUTED}.\n")
    original = raw.read_bytes()
    report = raw_screening.screen_raw_intake(kb, cfg)
    assert report.failed == [RAW_REL]
    assert raw.read_bytes() == original
    assert open_store(cfg, kb).snapshot("snap") is None
    assert not _committed(kb, RAW_REL)


# ---------------------------------------------------------------------------
# The stuck-file ledger across the rewrite
# ---------------------------------------------------------------------------


def test_the_stuck_ledger_counts_a_rewritten_file_as_the_same_file(tmp_path: Path) -> None:
    from athenaeum import librarian
    from athenaeum.models import RawFile

    cfg = _cfg(tmp_path / "vault")
    kb = _kb(tmp_path, cfg)
    raw_path = _put_raw(kb, f"Mail {ROUTED}.\n")

    def _raw() -> RawFile:
        return RawFile(path=raw_path, source="src", timestamp="", uuid8="")

    ledger = librarian._load_stuck_ledger(kb / "wiki")
    librarian._record_stuck_failure(ledger, _raw(), error="E", action="a", threshold=5)
    librarian._write_stuck_ledger(kb / "wiki", ledger)
    quarantine = {"src/" + raw_path.name: {
        "hash": librarian._quarantine_content_hash(_raw(), bound="llm_calls"),
        "violations": 1,
    }}
    librarian._write_quarantine_candidates(kb / "wiki", quarantine)

    report = raw_screening.screen_raw_intake(kb, cfg)
    assert len(report.rewritten) == 1
    librarian._rekey_content_records(kb / "wiki", report.rewritten)

    ledger = librarian._load_stuck_ledger(kb / "wiki")
    entry = librarian._record_stuck_failure(ledger, _raw(), error="E", action="a", threshold=2)
    assert ledger["src/" + raw_path.name]["failures"] == 2
    assert entry is not None  # crossed the threshold: it was counted as the same file
    q = librarian._load_quarantine_candidates(kb / "wiki")["src/" + raw_path.name]
    assert q["hash"] == librarian._quarantine_content_hash(_raw(), bound="llm_calls")


# ---------------------------------------------------------------------------
# A full run
# ---------------------------------------------------------------------------


def _bare_remote(tmp_path: Path, kb: Path) -> Path:
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)
    _git(kb, "remote", "add", "origin", str(bare))
    _git(kb, "push", "-q", "-u", "origin", "HEAD")
    return bare


def _summary_segment(caplog: pytest.LogCaptureFixture) -> str:
    for rec in caplog.records:
        msg = rec.getMessage()
        if "librarian-run-summary" in msg and "raw-screen" in msg:
            return msg
    return ""


def test_sentinel_with_the_hold_on_reaches_no_commit_of_either_repository(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from athenaeum.librarian import run

    vault = tmp_path / "vault"
    cfg = _cfg(vault)
    kb = _kb(tmp_path, cfg)
    bare = _bare_remote(tmp_path, kb)
    _put_raw(kb, f"Mail {ROUTED} and {DISCARDED}.\n")  # written straight into raw intake
    caplog.set_level(logging.INFO)
    run(
        raw_root=kb / "raw",
        wiki_root=kb / "wiki",
        knowledge_root=kb,
        push_after_run=True,
    )
    assert ROUTED not in _all_history(kb) and DISCARDED not in _all_history(kb)
    assert ROUTED not in _all_history(bare) and DISCARDED not in _all_history(bare)
    assert _git(kb, "rev-parse", "HEAD") == _git(bare, "rev-parse", "HEAD")  # the push happened
    assert [ROUTED in p.read_text(encoding="utf-8") for p in _vault_records(vault)] == [True]
    assert _committed(kb, RAW_REL)  # the screened text was committed


def test_sentinel_with_the_hold_off_and_a_failing_vault_commits_the_file_and_names_it(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from athenaeum.librarian import run

    vault = tmp_path / "vault"
    vault.write_text("a file where the vault directory must go", encoding="utf-8")
    cfg = _cfg(vault, hold=False)
    kb = _kb(tmp_path, cfg)
    _bare_remote(tmp_path, kb)
    _put_raw(kb, f"Mail {ROUTED}.\n")
    caplog.set_level(logging.INFO)
    run(
        raw_root=kb / "raw",
        wiki_root=kb / "wiki",
        knowledge_root=kb,
        push_after_run=True,
    )
    assert ROUTED in _git(kb, "show", f"HEAD:{RAW_REL}")
    assert f"unscreened_staged={RAW_REL}" in _summary_segment(caplog)


def test_run_summary_counts_held_files(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    from athenaeum.librarian import run

    cfg = _cfg(tmp_path / "vault", extra_routes=[{"page_type": "secret_page", "to": ["vault"]}])
    kb = _kb(tmp_path, cfg)
    _put_raw(kb, "no declared type here\n")
    caplog.set_level(logging.INFO)
    run(raw_root=kb / "raw", wiki_root=kb / "wiki", knowledge_root=kb)
    segment = _summary_segment(caplog)
    assert "raw-screen" in segment and "held=1" in segment


def test_a_run_with_no_rule_commits_raw_files_untouched_and_reports_no_phase(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from athenaeum.librarian import run

    kb = _kb(tmp_path, None)
    raw = _put_raw(kb, f"Mail {ROUTED}.\n")
    original = raw.read_bytes()
    caplog.set_level(logging.INFO)
    run(raw_root=kb / "raw", wiki_root=kb / "wiki", knowledge_root=kb)
    assert raw.read_bytes() == original
    assert _git(kb, "show", f"HEAD:{RAW_REL}") == original.decode("utf-8")
    assert _summary_segment(caplog) == ""
    assert not (tmp_path / "cache" / raw_screening.LEDGER_DIRNAME).exists()


def test_screening_runs_before_the_first_snapshot_of_a_run(tmp_path: Path) -> None:
    from athenaeum.librarian import run

    cfg = _cfg(tmp_path / "vault")
    kb = _kb(tmp_path, cfg)
    _put_raw(kb, f"Mail {ROUTED}.\n")
    head = _git(kb, "rev-parse", "HEAD").strip()
    run(raw_root=kb / "raw", wiki_root=kb / "wiki", knowledge_root=kb, merge_only=True)
    assert ROUTED not in _all_history(kb)
    log = _git(kb, "log", "--format=%s", f"{head}..HEAD")
    assert "pre-processing snapshot" in log or log == ""


# ---------------------------------------------------------------------------
# The notice, the warnings, status, doctor
# ---------------------------------------------------------------------------


def _git_tree(tmp_path: Path, cfg: dict) -> Path:
    return _kb(tmp_path, cfg)


def test_notice_with_the_hold_on(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path / "vault")
    kb = _git_tree(tmp_path, cfg)
    messages = raw_screening.hold_messages(cfg, kb)
    assert [m.kind for m in messages] == ["notice", "warning"]
    assert messages[0].text == raw_screening.NOTICE_TEXT
    assert "not snapshotted before they are processed" in messages[0].text
    assert "kept only in their vault, or nowhere for a discard" in messages[0].text
    assert messages[1].text == raw_screening.REACH_TEXT  # raw sits inside a git working tree


def test_warning_with_the_hold_off_and_raw_in_a_git_tree(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path / "vault", hold=False)
    kb = _git_tree(tmp_path, cfg)
    texts = [m.text for m in raw_screening.hold_messages(cfg, kb)]
    assert texts == [raw_screening.HOLD_OFF_WARNING_TEXT, raw_screening.REACH_TEXT]
    assert "can leak into that history" in texts[0]


def test_no_hold_off_warning_when_raw_has_no_history_and_the_reach_line_still_applies(
    tmp_path: Path,
) -> None:
    cfg = _cfg(tmp_path / "vault", hold=False)
    kb = tmp_path / "plain"
    (kb / "raw").mkdir(parents=True)
    texts = [m.text for m in raw_screening.hold_messages(cfg, kb)]
    assert texts == [raw_screening.REACH_TEXT]  # not a git repository


def test_no_messages_without_a_rule(tmp_path: Path) -> None:
    kb = _kb(tmp_path, None)
    assert raw_screening.hold_messages({}, kb) == []


def test_the_rule_load_path_prints_once_per_process(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    cfg = _cfg(tmp_path / "vault")
    kb = _kb(tmp_path, cfg)
    caplog.set_level(logging.WARNING)
    raw_screening.emit_hold_messages(cfg, kb)
    raw_screening.emit_hold_messages(cfg, kb)
    texts = [r.getMessage() for r in caplog.records]
    assert texts.count(raw_screening.NOTICE_TEXT) == 1


def test_a_run_prints_the_notice_when_the_rule_is_loaded(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from athenaeum.librarian import run

    cfg = _cfg(tmp_path / "vault")
    kb = _kb(tmp_path, cfg)
    caplog.set_level(logging.WARNING)
    run(raw_root=kb / "raw", wiki_root=kb / "wiki", knowledge_root=kb, merge_only=True)
    assert raw_screening.NOTICE_TEXT in [r.getMessage() for r in caplog.records]


def test_storage_status_prints_the_same_text(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from athenaeum._cmd_storage import cmd_storage

    cfg = _cfg(tmp_path / "vault")
    kb = _kb(tmp_path, cfg)
    assert cmd_storage(argparse.Namespace(storage_target="status", path=kb, json=False)) == 0
    captured = capsys.readouterr()
    messages = raw_screening.hold_messages(load_config(kb), kb)
    for message in messages:
        stream = captured.out if message.kind == "notice" else captured.err
        assert message.text in stream


def test_status_changes_nothing(tmp_path: Path) -> None:
    from athenaeum._cmd_storage import cmd_storage

    cfg = _cfg(tmp_path / "vault")
    kb = _kb(tmp_path, cfg)
    _put_raw(kb, f"Mail {ROUTED}.\n")
    before = _git(kb, "status", "--porcelain")
    cmd_storage(argparse.Namespace(storage_target="status", path=kb, json=False))
    assert _git(kb, "status", "--porcelain") == before


def test_doctor_lists_committed_raw_files_by_path_and_class_only(tmp_path: Path) -> None:
    from athenaeum import doctor

    cfg = _cfg(tmp_path / "vault", hold=False)
    kb = _kb(tmp_path, cfg)
    _put_raw(kb, f"Mail {ROUTED} and {DISCARDED}.\n")
    open_store(cfg, kb).snapshot("snap")  # hold off: committed as is
    result = doctor.check_raw_values_in_history(cfg, kb)
    assert result.status == "warn"
    assert RAW_REL in result.detail and "pii" in result.detail and "tokenish" in result.detail
    assert ROUTED not in result.detail and DISCARDED not in result.detail
    assert ROUTED not in result.fix


def test_doctor_is_ok_when_no_committed_raw_file_holds_a_value(tmp_path: Path) -> None:
    from athenaeum import doctor

    cfg = _cfg(tmp_path / "vault")
    kb = _kb(tmp_path, cfg)
    _put_raw(kb, f"Mail {ROUTED}.\n")
    raw_screening.screen_raw_intake(kb, cfg)
    open_store(cfg, kb).snapshot("snap")
    assert doctor.check_raw_values_in_history(cfg, kb).status == "ok"
    assert doctor.check_raw_values_in_history({}, kb).status == "ok"


def test_config_reference_documents_the_setting() -> None:
    raw = (Path(__file__).resolve().parent.parent / "docs/reference/configuration.md").read_text(
        encoding="utf-8"
    )
    text = " ".join(raw.split())
    assert "librarian.hold_unscreened_raw" in text
    for message in (
        raw_screening.NOTICE_TEXT,
        raw_screening.HOLD_OFF_WARNING_TEXT,
        raw_screening.REACH_TEXT,
    ):
        assert message in text
    for fact in ("raw/_stores/", "raw/answers/", "storage.history: none", "sync client"):
        assert fact in text
