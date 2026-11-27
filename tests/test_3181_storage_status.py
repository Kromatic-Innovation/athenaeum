# SPDX-License-Identifier: Apache-2.0
"""``athenaeum storage status``: locations, routes, vaults and who may read them (athenaeum#3181).

All values are synthetic markers.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from athenaeum import raw_screening, store_rights
from athenaeum._cmd_storage import cmd_storage
from athenaeum.init import init_knowledge_dir
from athenaeum.storage_status import (
    MACHINE_ACCESS_LINE,
    OWNER_ONLY,
    VAULT_IN_GIT_WARNING,
    build_status,
)

ISS = "https://git.example.net"
ROUTED = "zz-routed-3181@example.invalid"
_NOT_CORPUS = {"embedded": False, "recallable": False, "merge_eligible": False}
#: No law, regulation or standard may be named in the report.
_FORBIDDEN = re.compile(
    r"\b(gdpr|hipaa|ccpa|cpra|sox|glba|ferpa|pci|dss|soc ?2|iso ?\d+|nist|fedramp|"
    r"law|laws|legal|regulation|regulations|regulatory|statute|compliance|compliant|standard)\b",
    re.IGNORECASE,
)


@pytest.fixture(autouse=True)
def _env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATHENAEUM_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(raw_screening, "_EMITTED", set())
    store_rights.reset_run_rights()


def _kb(tmp_path: Path, cfg: dict[str, Any] | None = None) -> Path:
    kb = init_knowledge_dir(tmp_path / "kb")
    path = kb / "athenaeum.yaml"
    merged = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    for key, value in (cfg or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key].update(value)
        else:
            merged[key] = value
    path.write_text(yaml.safe_dump(merged), encoding="utf-8")
    subprocess.run(["git", "-C", str(kb), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(kb), "commit", "-qm", "cfg"], check=True, capture_output=True)
    return kb


def _vault(root: str) -> dict[str, Any]:
    return {
        "backing_store": "markdown",
        "surface_root": root,
        "corpus_policy": _NOT_CORPUS,
        "sensitive": True,
    }


def _cfg(tmp_path: Path) -> dict[str, Any]:
    return {
        "owner": {"asserter": {"iss": ISS, "sub": "owner"}},
        "storage": {
            "adapters": {
                "outside": _vault(str(tmp_path / "outside-vault")),
                "inside": _vault("inside-vault"),
                "archive": {
                    "backing_store": "markdown",
                    "surface_root": str(tmp_path / "archive"),
                    "corpus_policy": _NOT_CORPUS,
                    "purgeable": False,
                    "read_only": True,
                },
            },
            "routes": [
                {"sensitivity_class": "pii", "to": ["outside"]},
                {"page_type": "reference", "fields": ["ssn_like"], "to": ["inside"]},
                {"page_type": "reference", "fields": ["scratch"], "to": []},
            ],
        },
    }


def _declare(root: Path, readers: dict[str, Any]) -> None:
    decl = {
        "version": 1,
        "store_id": "st_q7m2xk4vwz5d",
        "groups": {"team": [{"iss": ISS, "sub": "mem"}]},
        "roles": {
            "reader": {"rights": ["read"], "members": readers.get("members", [])},
            "proposer": {"rights": ["propose"], "members": [{"iss": ISS, "sub": "prop-only"}]},
        },
    }
    d = root / "_store"
    d.mkdir(parents=True)
    (d / "access.yaml").write_text(yaml.safe_dump(decl), encoding="utf-8")


def _run(kb: Path, *, as_json: bool = False) -> tuple[int, str, str]:
    import contextlib
    import io

    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = cmd_storage(argparse.Namespace(storage_target="status", path=kb, json=as_json))
    return rc, out.getvalue(), err.getvalue()


def _tree(*roots: Path) -> dict[str, tuple[int, int]]:
    seen: dict[str, tuple[int, int]] = {}
    for root in roots:
        if not root.exists():
            seen[str(root)] = (-1, -1)
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames.sort()
            for name in [*dirnames, *filenames]:
                p = Path(dirpath) / name
                st = p.lstat()
                seen[str(p)] = (st.st_size, st.st_mtime_ns)
    return seen


def test_each_location_reports_its_flags(tmp_path: Path) -> None:
    kb = _kb(tmp_path, _cfg(tmp_path))
    (tmp_path / "outside-vault").mkdir()
    _declare(tmp_path / "outside-vault", {})
    status = build_status(_load(kb), kb)
    rows = {r["name"]: r for r in status["locations"]}
    assert {"wiki-markdown-embedded", "excluded", "outside", "inside", "archive"} <= set(rows)
    outside, inside, archive = rows["outside"], rows["inside"], rows["archive"]
    assert outside["root"] == str(tmp_path / "outside-vault")
    assert outside["sensitive"] is True and outside["in_corpus"] is False
    assert outside["inside_git_working_tree"] is False
    assert outside["has_store_declaration"] is True
    assert inside["root"] == str(kb / "inside-vault")
    assert inside["inside_git_working_tree"] is True and inside["has_store_declaration"] is False
    assert archive["read_only"] is True and archive["purgeable"] is False
    assert archive["sensitive"] is False
    assert rows["wiki-markdown-embedded"]["in_corpus"] is True
    assert rows["wiki-markdown-embedded"]["purgeable"] is True


def _load(kb: Path) -> dict[str, Any]:
    from athenaeum.config import load_config

    return load_config(kb)


def test_each_rule_lists_page_type_fields_targets_or_discarded(tmp_path: Path) -> None:
    kb = _kb(tmp_path, _cfg(tmp_path))
    status = build_status(_load(kb), kb)
    by_rule = {r["rule"]: r for r in status["routes"]}
    assert by_rule["storage.routes[0]"]["sensitivity_class"] == "pii"
    assert by_rule["storage.routes[0]"]["targets"] == ["outside"]
    assert by_rule["storage.routes[1]"]["page_type"] == "reference"
    assert by_rule["storage.routes[1]"]["fields"] == ["ssn_like"]
    assert by_rule["storage.routes[1]"]["targets"] == ["inside"]
    assert by_rule["storage.routes[2]"]["discarded"] is True
    assert by_rule["storage.routes[2]"]["targets"] == []
    rc, out, _ = _run(kb)
    assert rc == 0
    assert "storage.routes[2]: reference; fields scratch; discarded" in out
    assert "storage.routes[1]: reference; fields ssn_like; inside" in out


def test_builtin_contact_fields_without_a_rule_are_kept_on_the_page(tmp_path: Path) -> None:
    kb = _kb(tmp_path)
    status = build_status(_load(kb), kb)
    assert [r["field"] for r in status["contact_fields"]] == [
        "emails",
        "phones",
        "former_emails",
        "alt_emails",
    ]
    assert {r["disposition"] for r in status["contact_fields"]} == {"kept on the page"}
    _, out, _ = _run(kb)
    assert "person.emails: kept on the page" in out


def test_a_contact_field_with_a_rule_shows_the_rule_not_kept(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    cfg["storage"]["routes"].append({"page_type": "person", "fields": ["emails"], "to": []})
    kb = _kb(tmp_path, cfg)
    rows = {r["field"]: r for r in build_status(_load(kb), kb)["contact_fields"]}
    assert rows["emails"]["disposition"] == "discarded"
    assert rows["phones"]["disposition"] == "kept on the page"


def test_vault_readers_come_from_the_declaration_through_the_predicate(tmp_path: Path) -> None:
    kb = _kb(tmp_path, _cfg(tmp_path))
    vault = tmp_path / "outside-vault"
    _declare(
        vault,
        {"members": [{"iss": ISS, "sub": "ed"}, "group:team"]},
    )
    store_rights.reset_run_rights()
    vaults = {v["name"]: v["readers"] for v in build_status(_load(kb), kb)["vaults"]}
    readers = vaults["outside"]
    assert readers["declaration"] == "valid" and readers["owner_only"] is False
    assert readers["principals"] == [[ISS, "ed"]]  # the propose-only principal does not read
    assert readers["groups"] == ["team"]
    assert vaults["inside"]["owner_only"] is True  # no declaration
    assert vaults["excluded"]["owner_only"] is True
    _, out, _ = _run(kb)
    assert f"outside: {ISS} | ed; group team" in out
    assert f"inside: {OWNER_ONLY}" in out


def test_an_invalid_declaration_reads_as_owner_only(tmp_path: Path) -> None:
    kb = _kb(tmp_path, _cfg(tmp_path))
    d = tmp_path / "outside-vault" / "_store"
    d.mkdir(parents=True)
    (d / "access.yaml").write_text("version: 1\nstore_id: not-an-id\n", encoding="utf-8")
    store_rights.reset_run_rights()
    readers = {v["name"]: v["readers"] for v in build_status(_load(kb), kb)["vaults"]}["outside"]
    assert readers["declaration"] == "invalid" and readers["owner_only"] is True


def test_the_history_setting_and_what_it_means(tmp_path: Path) -> None:
    kb = _kb(tmp_path)
    git = build_status(_load(kb), kb)["history"]
    assert git["declared"] == "git"
    assert "restore point" in git["restore"] and "undone" in git["retirement"]
    cfg = {"storage": {"history": "none"}}
    plain = build_status(cfg, kb)["history"]
    assert plain["declared"] == "none"
    assert "no restore point" in plain["restore"].lower()
    assert "no way to get it back" in plain["retirement"]


def test_a_vault_inside_a_git_working_tree_warns_on_stderr_and_in_json(tmp_path: Path) -> None:
    kb = _kb(tmp_path, _cfg(tmp_path))
    expected = VAULT_IN_GIT_WARNING.format(name="inside")
    assert "erasure does not reach its history" in expected
    rc, out, err = _run(kb)
    assert rc == 0 and f"warning: {expected}" in err
    assert VAULT_IN_GIT_WARNING.format(name="outside") not in err
    rc, out, err = _run(kb, as_json=True)
    payload = json.loads(out)
    assert rc == 0 and expected in payload["warnings"]
    assert f"warning: {expected}" in err
    assert "inside" in {v["name"] for v in payload["vaults"]}


def test_the_raw_intake_warning_counts_unscreened_and_held_files(tmp_path: Path) -> None:
    kb = _kb(tmp_path, _cfg(tmp_path))
    raw = kb / "raw" / "src"
    raw.mkdir(parents=True)
    (raw / "a.md").write_text(f"Mail {ROUTED}.\n", encoding="utf-8")
    (raw / "b.md").write_text("plain\n", encoding="utf-8")
    cfg = _load(kb)
    status = build_status(cfg, kb)
    assert status["raw_intake"] == {
        "inside_git_working_tree": True,
        "knowledge_dir_is_git_repository": True,
        "hold_on": True,
        "unscreened": 2,
        "held": 2,
    }
    needle = "2 raw file(s) are unscreened, 2 held out of commits"
    assert any(needle in w for w in status["warnings"])
    assert raw_screening.REACH_TEXT in status["warnings"]
    assert raw_screening.NOTICE_TEXT in status["notices"]
    raw_screening.screen_raw_intake(kb, cfg)
    assert build_status(cfg, kb)["raw_intake"]["unscreened"] == 0


def test_the_raw_intake_warning_with_the_hold_off(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    cfg["librarian"] = {"hold_unscreened_raw": False}
    kb = _kb(tmp_path, cfg)
    (kb / "raw" / "src").mkdir(parents=True)
    (kb / "raw" / "src" / "a.md").write_text("x\n", encoding="utf-8")
    status = build_status(_load(kb), kb)
    assert status["raw_intake"]["held"] == 0 and status["raw_intake"]["unscreened"] == 1
    assert raw_screening.HOLD_OFF_WARNING_TEXT in status["warnings"]
    assert status["notices"] == []


def test_the_reach_sentence_is_always_present(tmp_path: Path) -> None:
    kb = _kb(tmp_path)
    status = build_status({}, kb)
    assert raw_screening.REACH_TEXT in status["warnings"]
    assert status["raw_intake"]["unscreened"] == 0 and status["raw_intake"]["held"] == 0


def test_the_machine_access_line_is_fixed(tmp_path: Path) -> None:
    kb = _kb(tmp_path, _cfg(tmp_path))
    assert build_status(_load(kb), kb)["machine_access"] == MACHINE_ACCESS_LINE
    _, out, _ = _run(kb)
    assert MACHINE_ACCESS_LINE in out.splitlines()
    assert "any process running as this account can read a vault's files directly" in out


def test_json_output_is_complete(tmp_path: Path) -> None:
    kb = _kb(tmp_path, _cfg(tmp_path))
    rc, out, _ = _run(kb, as_json=True)
    payload = json.loads(out)
    assert rc == 0
    assert set(payload) == {
        "history",
        "locations",
        "routes",
        "contact_fields",
        "vaults",
        "raw_intake",
        "machine_access",
        "notices",
        "warnings",
    }


def test_the_command_changes_nothing(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    kb = _kb(tmp_path, cfg)
    (kb / "raw" / "src").mkdir(parents=True)
    (kb / "raw" / "src" / "a.md").write_text(f"Mail {ROUTED}.\n", encoding="utf-8")
    vault = tmp_path / "outside-vault"
    vault.mkdir()
    _declare(vault, {"members": [{"iss": ISS, "sub": "ed"}]})
    cache, state = tmp_path / "cache", tmp_path / "state"
    cache.mkdir()
    state.mkdir()
    (cache / "f").write_text("c", encoding="utf-8")
    (state / "f").write_text("s", encoding="utf-8")
    git_before = subprocess.run(
        ["git", "-C", str(kb), "status", "--porcelain", "-uall"], capture_output=True, text=True
    ).stdout
    before = _tree(kb, cache, state, vault, tmp_path / "archive")
    _run(kb)
    _run(kb, as_json=True)
    assert _tree(kb, cache, state, vault, tmp_path / "archive") == before
    assert (
        subprocess.run(
            ["git", "-C", str(kb), "status", "--porcelain", "-uall"],
            capture_output=True,
            text=True,
        ).stdout
        == git_before
    )


def test_the_output_names_no_law_regulation_or_standard(tmp_path: Path) -> None:
    kb = _kb(tmp_path, _cfg(tmp_path))
    (tmp_path / "outside-vault").mkdir()
    _declare(tmp_path / "outside-vault", {"members": [{"iss": ISS, "sub": "ed"}, "group:team"]})
    (kb / "raw" / "src").mkdir(parents=True)
    (kb / "raw" / "src" / "a.md").write_text("x\n", encoding="utf-8")
    for as_json in (False, True):
        _, out, err = _run(kb, as_json=as_json)
        assert _FORBIDDEN.findall(out + err) == []
    plain = build_status({"storage": {"history": "none"}}, kb)
    assert _FORBIDDEN.findall(json.dumps(plain)) == []


def test_the_report_holds_no_logic_of_its_own() -> None:
    import ast
    import inspect

    from athenaeum import storage_status

    tree = ast.parse(inspect.getsource(storage_status))
    called = {
        n.func.id
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    # Every question is put to the resolver, the location list or the predicate.
    assert {
        "available_adapters",
        "configured_routes",
        "route_for",
        "can",
        "load_store_declaration",
        "resolve_storage_history",
        "inside_git_repository",
        "unscreened_raw_files",
        "hold_messages",
    } <= called
    assert "open" not in called and "write_text" not in inspect.getsource(storage_status)


def test_status_runs_with_no_configuration_at_all(tmp_path: Path) -> None:
    kb = _kb(tmp_path)
    rc, out, _ = _run(kb)
    assert rc == 0 and "History: git" in out and "Routes:\n  (none configured)" in out
