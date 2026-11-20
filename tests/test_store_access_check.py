# SPDX-License-Identifier: Apache-2.0
"""``athenaeum store access check`` (athenaeum#2991, access S5)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from athenaeum import store_access_evidence as evidence
from athenaeum.cli import main
from athenaeum.store import ARTIFACT_REGISTRY

ISS = "https://git.example.net"
ME = {"iss": ISS, "sub": "1042"}
OTHER = {"iss": ISS, "sub": "1077"}
BOT = {"iss": ISS, "sub": "svc-librarian", "type": "software_agent"}
BENCH = "git@git.example.net:acme/bench.git"


def _decl(roles: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {"version": 1, "store_id": "st_q7m2xk4vwz5d", "roles": roles, **extra}


SOLE_WRITER = _decl(
    {"editor": {"rights": ["read", "propose", "compile", "answer"], "members": [ME]}}
)


def _mount(
    tmp_path: Path, declaration: dict[str, Any], *, read_only: bool = False, asserter: Any = ME
) -> Path:
    """A knowledge root with one declared store mounted as a git repository."""
    kr = tmp_path / "kr"
    pages = tmp_path / "pages"
    (pages / "_store").mkdir(parents=True)
    (pages / "_store" / "access.yaml").write_text(yaml.safe_dump(declaration), encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(pages)], check=True)
    kr.mkdir()
    cfg = {
        "owner": {"asserter": asserter},
        "storage": {
            "adapters": {
                "shared": {
                    "backing_store": "markdown",
                    "surface_root": str(pages),
                    "read_only": read_only,
                }
            }
        },
    }
    (kr / "athenaeum.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return kr


def _run(kr: Path, capsys: pytest.CaptureFixture[str]) -> tuple[int, dict[str, Any]]:
    code = main(["store", "access", "check", "--path", str(kr), "--json"])
    return code, json.loads(capsys.readouterr().out)


def _store(report: dict[str, Any]) -> dict[str, Any]:
    (row,) = [s for s in report["stores"] if s["store"] == "shared"]
    return row


def test_counter_example_sole_writer_passes(tmp_path, capsys) -> None:
    code, report = _run(_mount(tmp_path, SOLE_WRITER), capsys)
    row = _store(report)
    assert code == 0 and report["ok"] is True
    assert row["rights"] == ["read", "propose", "compile", "answer"]
    assert row["differences"] == [] and row["refusals"] == []


def test_reports_rights_and_read_only(tmp_path, capsys) -> None:
    _, report = _run(_mount(tmp_path, SOLE_WRITER, read_only=True), capsys)
    row = _store(report)
    assert row["read_only"] is True and "compile" in row["rights"]


def test_never_pushed_reports_not_yet_observed(tmp_path, capsys) -> None:
    _, report = _run(_mount(tmp_path, SOLE_WRITER), capsys)
    pages = _store(report)["locations"][0]
    assert pages["push"] == "not yet observed" and pages["fetch"] == "not yet observed"


def test_records_evidence_and_reports_it(tmp_path, capsys) -> None:
    kr = _mount(tmp_path, SOLE_WRITER)
    pages = tmp_path / "pages"
    evidence.record_fetch(pages, "origin", True)
    evidence.record_push(pages, "origin", True)
    code, report = _run(kr, capsys)
    loc = _store(report)["locations"][0]
    assert (loc["fetch"], loc["push"]) == ("ok", "accepted")
    assert code == 0


def test_check_makes_no_push_and_writes_nothing(tmp_path, capsys, monkeypatch) -> None:
    kr = _mount(tmp_path, SOLE_WRITER)

    real_run = subprocess.run

    def guard(cmd: Any, *a: Any, **k: Any) -> Any:
        assert cmd[:2] not in (["git", "push"], ["git", "fetch"]), cmd
        return real_run(cmd, *a, **k)

    monkeypatch.setattr(subprocess, "run", guard)
    before = sorted(p for p in tmp_path.rglob("*"))
    code, _ = _run(kr, capsys)
    assert code == 0 and sorted(p for p in tmp_path.rglob("*")) == before


def test_difference_push_accepted_without_compile(tmp_path, capsys) -> None:
    decl = _decl(
        {
            "editor": {"rights": ["read", "compile"], "members": [OTHER]},
            "staff": {"rights": ["read"], "members": [ME]},
        }
    )
    kr = _mount(tmp_path, decl, read_only=True)
    evidence.record_push(tmp_path / "pages", "origin", True)
    code, report = _run(kr, capsys)
    assert code == 2
    assert any("push was accepted" in d for d in _store(report)["differences"])


def test_difference_read_only_false_without_compile(tmp_path, capsys) -> None:
    decl = _decl(
        {
            "editor": {"rights": ["read", "compile"], "members": [OTHER]},
            "staff": {"rights": ["read"], "members": [ME]},
        }
    )
    code, report = _run(_mount(tmp_path, decl, read_only=False), capsys)
    assert code == 2
    assert any("read_only is false" in d for d in _store(report)["differences"])


def test_difference_host_refused_push_despite_compile(tmp_path, capsys) -> None:
    kr = _mount(tmp_path, SOLE_WRITER)
    evidence.record_push(tmp_path / "pages", "origin", False, "remote: Permission denied (403)")
    code, report = _run(kr, capsys)
    assert code == 2
    assert any("refused the last push" in d for d in _store(report)["differences"])


def test_contended_push_is_not_a_difference(tmp_path, capsys) -> None:
    kr = _mount(tmp_path, SOLE_WRITER)
    evidence.record_push(tmp_path / "pages", "origin", False, "contended")
    code, _ = _run(kr, capsys)
    assert code == 0


def test_refuses_two_compiling_principals_without_integrate(tmp_path, capsys) -> None:
    decl = _decl({"editor": {"rights": ["read", "compile"], "members": [ME, OTHER]}})
    code, report = _run(_mount(tmp_path, decl), capsys)
    assert code == 2 and any("one-writer" in r for r in _store(report)["refusals"])


def test_integrate_accepts_two_compiling_principals(tmp_path, capsys) -> None:
    decl = _decl(
        {"editor": {"rights": ["read", "compile"], "members": [ME, OTHER]}},
        sync={"mode": "integrate"},
    )
    code, _ = _run(_mount(tmp_path, decl), capsys)
    assert code == 0


def test_two_compilers_through_a_static_group_are_refused(tmp_path, capsys) -> None:
    decl = _decl(
        {"editor": {"rights": ["read", "compile"], "members": ["group:leads"]}},
        groups={"leads": [ME, OTHER]},
    )
    code, report = _run(_mount(tmp_path, decl), capsys)
    assert code == 2 and any("one-writer" in r for r in _store(report)["refusals"])


def test_refuses_answer_without_compile_and_no_bench(tmp_path, capsys) -> None:
    decl = _decl(
        {
            "editor": {"rights": ["read", "compile"], "members": [ME]},
            "steward": {"rights": ["read", "answer"], "members": [OTHER]},
        }
    )
    code, report = _run(_mount(tmp_path, decl), capsys)
    assert code == 2 and any("no bench" in r for r in _store(report)["refusals"])


def test_answer_without_compile_passes_with_a_bench(tmp_path, capsys) -> None:
    decl = _decl(
        {
            "editor": {"rights": ["read", "compile"], "members": [ME]},
            "steward": {"rights": ["read", "answer"], "members": [OTHER]},
        },
        locations={"bench": BENCH},
    )
    code, report = _run(_mount(tmp_path, decl), capsys)
    assert code == 0
    assert any(loc["role"] == "bench" for loc in _store(report)["locations"])


def test_refuses_software_only_librarian_with_no_person_answering(tmp_path, capsys) -> None:
    decl = _decl(
        {"librarian": {"rights": ["read", "compile", "answer"], "members": [BOT]}},
    )
    code, report = _run(_mount(tmp_path, decl), capsys)
    assert code == 2 and any("software agent" in r for r in _store(report)["refusals"])


def test_software_librarian_with_a_person_answering_passes(tmp_path, capsys) -> None:
    decl = _decl(
        {
            "librarian": {"rights": ["read", "compile"], "members": [BOT]},
            "steward": {"rights": ["read", "answer"], "members": [ME]},
        },
        locations={"bench": BENCH},
    )
    code, _ = _run(_mount(tmp_path, decl, read_only=True), capsys)
    assert code == 0


def test_invalid_declaration_is_refused(tmp_path, capsys) -> None:
    code, report = _run(_mount(tmp_path, {"version": 1}), capsys)
    assert code == 2 and _store(report)["refusals"]


def test_no_declared_store_exits_zero(tmp_path, capsys) -> None:
    kr = tmp_path / "kr"
    kr.mkdir()
    code, report = _run(kr, capsys)
    assert code == 0 and report["stores"] == []


def test_plain_text_output(tmp_path, capsys) -> None:
    code = main(["store", "access", "check", "--path", str(_mount(tmp_path, SOLE_WRITER))])
    out = capsys.readouterr().out
    assert code == 0 and "push: not yet observed" in out and "rights: read, propose" in out


def test_evidence_file_is_registered_machine_local() -> None:
    (entry,) = [a for a in ARTIFACT_REGISTRY if a.name == "store-access-evidence"]
    assert entry.persistence_class == "operational"
    assert entry.operational_scope == "machine-local"


def test_unknown_principal_type_is_invalid(tmp_path, capsys) -> None:
    bad = {"iss": ISS, "sub": "x", "type": "robot"}
    decl = _decl({"editor": {"rights": ["read", "compile"], "members": [bad]}})
    code, report = _run(_mount(tmp_path, decl), capsys)
    assert code == 2 and any("type" in r for r in _store(report)["refusals"])


def test_bench_evidence_matches_by_remote_url(tmp_path, capsys) -> None:
    decl = _decl(
        {
            "editor": {"rights": ["read", "compile"], "members": [ME]},
            "steward": {"rights": ["read", "answer"], "members": [OTHER]},
        },
        locations={"bench": BENCH},
    )
    kr = _mount(tmp_path, decl)
    bench = tmp_path / "bench"
    subprocess.run(["git", "init", "-q", str(bench)], check=True)
    subprocess.run(["git", "-C", str(bench), "remote", "add", "origin", BENCH], check=True)
    evidence.record_fetch(bench, "origin", True)
    _, report = _run(kr, capsys)
    (loc,) = [x for x in _store(report)["locations"] if x["role"] == "bench"]
    assert (loc["fetch"], loc["push"]) == ("ok", "not yet observed")


@pytest.mark.parametrize(
    "detail",
    [
        "ssh: Could not resolve hostname git.example.net\n"
        "fatal: Could not read from remote repository.",
        "fatal: Could not read from remote repository.\n"
        "Please make sure you have the correct access rights",
        "ssh: connect to host git.example.net port 22: Connection timed out",
        "Writing objects: 100% (403/403), 401 bytes | 401.00 KiB/s, done.",
    ],
)
def test_transport_failure_is_not_a_refusal(tmp_path, capsys, detail) -> None:
    kr = _mount(tmp_path, SOLE_WRITER)
    evidence.record_push(tmp_path / "pages", "origin", False, detail)
    code, report = _run(kr, capsys)
    assert code == 0 and _store(report)["differences"] == []


@pytest.mark.parametrize(
    "detail",
    [
        "remote: Permission to acme/pages.git denied to someone.",
        "fatal: unable to access 'https://x/': The requested URL returned error: 403",
        "! [remote rejected] main -> main (protected branch hook declined)",
    ],
)
def test_clear_refusal_is_a_difference(tmp_path, capsys, detail) -> None:
    kr = _mount(tmp_path, SOLE_WRITER)
    evidence.record_push(tmp_path / "pages", "origin", False, detail)
    code, report = _run(kr, capsys)
    assert code == 2 and any("refused" in d for d in _store(report)["differences"])
