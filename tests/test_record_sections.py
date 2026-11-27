# SPDX-License-Identifier: Apache-2.0
"""Named sections on an entity's vault record (athenaeum#3179)."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Any

import pytest

from athenaeum import pii
from athenaeum.models import parse_frontmatter
from athenaeum.pii import (
    RecordSectionError,
    delete_record_section,
    read_entity,
    write_record_section,
)
from athenaeum.retire_targets import RetirePlan, RetireTarget, apply_plan
from athenaeum.status import format_status, status
from athenaeum.storage import StorageTargetError

UID = "person-alex"
BOTH = ("vault_a", "vault_b")
OFF = {"embedded": False, "recallable": False, "merge_eligible": False}
SENTINEL = "zz-payload-sentinel-zz"


def _vault(root: str) -> dict[str, Any]:
    return {
        "backing_store": "markdown",
        "surface_root": root,
        "corpus_policy": OFF,
        "sensitive": True,
    }


def _cfg(routes: bool = True) -> dict[str, Any]:
    out: dict[str, Any] = {
        "storage": {
            "adapters": {
                "vault_a": _vault("vault_a"),
                "vault_b": _vault("vault_b"),
                "vault_c": _vault("vault_c"),
            },
            "mapping": {"pii": "vault_a"},
        }
    }
    if routes:
        out["storage"]["routes"] = [
            {"page_type": "person", "fields": ["emails"], "to": ["vault_a", "vault_b"]}
        ]
    return out


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / "wiki").mkdir()
    (tmp_path / "wiki" / f"{UID}.md").write_text(
        f"---\nuid: {UID}\ntype: person\nname: Alex Doe\n---\nBody.\n", encoding="utf-8"
    )
    return tmp_path


def _meta(root: Path, vault: str, uid: str = UID) -> dict[str, Any]:
    return parse_frontmatter((root / vault / f"{uid}.md").read_text(encoding="utf-8"))[0]


def _write(root: Path, payload: Any = None, **kw: Any) -> bool:
    kw.setdefault("targets", ("vault_a", "vault_b"))
    return write_record_section(
        root,
        kw.pop("config", _cfg()),
        kw.pop("uid", UID),
        "test_section",
        "k1",
        {"v": SENTINEL} if payload is None else payload,
        **kw,
    )


def test_writes_to_every_target_and_is_idempotent(root: Path) -> None:
    assert _write(root) is True
    for vault in ("vault_a", "vault_b"):
        assert _meta(root, vault)["sections"] == {"test_section": {"k1": {"v": SENTINEL}}}
    before = (root / "vault_a" / f"{UID}.md").read_text(encoding="utf-8")
    assert _write(root) is False
    assert (root / "vault_a" / f"{UID}.md").read_text(encoding="utf-8") == before


def test_a_page_is_never_changed_and_an_entity_never_created(root: Path) -> None:
    page = root / "wiki" / f"{UID}.md"
    text = page.read_text(encoding="utf-8")
    _write(root)
    assert page.read_text(encoding="utf-8") == text
    with pytest.raises(RecordSectionError, match="never creates"):
        _write(root, uid="person-nobody")
    assert not (root / "vault_a" / "person-nobody.md").exists()


def test_unknown_section_is_an_error_and_names_the_registry(root: Path) -> None:
    with pytest.raises(RecordSectionError, match="test_section"):
        write_record_section(root, _cfg(), UID, "nope", "k", "x", targets=("vault_a",))
    with pytest.raises(RecordSectionError):
        delete_record_section(root, _cfg(), UID, "nope", targets=("vault_a",))
    assert "test_section" in pii.record_section_names()


def test_a_record_is_minted_only_where_a_rule_names_the_location(root: Path) -> None:
    with pytest.raises(StorageTargetError, match="vault_c"):
        _write(root, targets=("vault_c",))
    assert not (root / "vault_c" / f"{UID}.md").exists()
    with pytest.raises(StorageTargetError, match="vault_a"):
        _write(root, config=_cfg(routes=False), targets=("vault_a",))
    # An existing record in an unnamed location may still take a section.
    (root / "vault_c").mkdir()
    (root / "vault_c" / f"{UID}.md").write_text(f"---\nuid: {UID}\n---\n", encoding="utf-8")
    assert _write(root, targets=("vault_c",)) is True


def test_a_failing_target_is_named_and_a_rerun_completes_it(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = pii.write_surface_record
    left = {"n": 1}

    def flaky(path: Path, record: dict[str, Any], *, body: str = "") -> None:
        if "vault_b" in path.parts and left["n"]:
            left["n"] -= 1
            raise OSError("disk full")
        real(path, record, body=body)

    monkeypatch.setattr(pii, "write_surface_record", flaky)
    with pytest.raises(StorageTargetError, match="vault_b") as err:
        _write(root)
    assert err.value.target == "vault_b"
    assert (root / "vault_a" / f"{UID}.md").exists()
    assert not (root / "vault_b" / f"{UID}.md").exists()
    _write(root)
    assert _meta(root, "vault_b")["sections"] == _meta(root, "vault_a")["sections"]


def test_read_entity_returns_sections_only_under_the_flag(root: Path) -> None:
    _write(root)
    cfg = _cfg()
    shown = read_entity(root, cfg, UID, surface_class="pii", include_excluded=True)
    assert shown is not None
    assert shown.sections == {"test_section": {"k1": {"v": SENTINEL}}}
    assert shown.section_redactions == ()
    # The record's own fields are untouched by a section (no "sections" data field).
    assert "sections" not in shown.contact

    hidden = read_entity(root, cfg, UID, surface_class="pii", include_excluded=False)
    assert hidden is not None
    assert hidden.sections == {}
    assert [(m.field, m.value_count) for m in hidden.section_redactions] == [
        ("sections.test_section", 1)
    ]
    dumped = str(hidden.to_dict())
    assert SENTINEL not in dumped and "k1" not in dumped


def test_delete_removes_one_entry_or_the_whole_section_from_every_target(root: Path) -> None:
    cfg = _cfg()
    for key in ("k1", "k2"):
        write_record_section(root, cfg, UID, "test_section", key, key, targets=BOTH)
    assert delete_record_section(root, cfg, UID, "test_section", "k1", targets=BOTH)
    for vault in ("vault_a", "vault_b"):
        assert _meta(root, vault)["sections"] == {"test_section": {"k2": "k2"}}
    assert delete_record_section(root, cfg, UID, "test_section", targets=("vault_a", "vault_b"))
    for vault in ("vault_a", "vault_b"):
        assert "sections" not in _meta(root, vault)
    assert not delete_record_section(root, cfg, UID, "test_section", targets=("vault_a", "vault_b"))


def test_delete_names_an_unreachable_target(root: Path) -> None:
    with pytest.raises(StorageTargetError, match="vault_c"):
        delete_record_section(root, _cfg(), UID, "test_section", targets=("vault_c",))


def test_erasing_the_entity_removes_every_section_in_every_target(
    root: Path, tmp_path: Path
) -> None:
    cfg = _cfg()
    write_record_section(root, cfg, UID, "test_section", "k1", "p", targets=BOTH)
    write_record_section(root, cfg, UID, "test_section", "k2", "p", targets=BOTH)
    for args in (
        ("init", "-b", "develop"),
        ("config", "user.email", "t@example.com"),
        ("config", "user.name", "T"),
        ("add", "-A"),
        ("commit", "-m", "seed"),
    ):
        subprocess.run(["git", *args], cwd=root, capture_output=True, check=True)
    plan = RetirePlan(targets=[RetireTarget(uid=UID, store="none", paths=[])])
    report = apply_plan(root, plan, reason="test", cache_dir=tmp_path / "cache", config=cfg)
    assert report.errors == []
    # The later target is erased by the run; the primary's record is a contact
    # record the plan names.
    assert not (root / "vault_b" / f"{UID}.md").exists()
    primary = root / "vault_a" / f"{UID}.md"
    plan = RetirePlan(targets=[RetireTarget(uid=UID, store="contact", paths=[primary])])
    report = apply_plan(root, plan, reason="test", cache_dir=tmp_path / "cache", config=cfg)
    assert report.errors == []
    for vault in ("vault_a", "vault_b"):
        assert not (root / vault / f"{UID}.md").exists()


def test_the_payload_never_reaches_a_log(root: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    cfg = _cfg()
    _write(root)
    _write(root)
    delete_record_section(root, cfg, UID, "test_section", "k1", targets=("vault_a", "vault_b"))
    with pytest.raises(RecordSectionError) as err:
        write_record_section(root, cfg, UID, "nope", "k", SENTINEL, targets=("vault_a",))
    assert SENTINEL not in caplog.text and SENTINEL not in str(err.value)
    assert "section=test_section" in caplog.text and "key=k1" in caplog.text


def test_status_lists_the_registered_sections(tmp_path: Path) -> None:
    info = status(tmp_path)
    assert "test_section" in info["record_sections"]
    assert "test_section" in format_status(info)


def test_a_payload_the_record_cannot_hold_is_refused_before_any_write(root: Path) -> None:
    for bad in (("a", "b"), object()):
        with pytest.raises(StorageTargetError) as err:
            _write(root, payload=bad)
        assert "cannot be stored" in str(err.value.__cause__)
    assert not (root / "vault_a" / f"{UID}.md").exists()


def test_a_record_is_minted_only_for_a_rule_that_applies_to_the_entity_type(
    root: Path,
) -> None:
    cfg = _cfg()
    cfg["storage"]["routes"] = [{"page_type": "vendor", "fields": ["emails"], "to": ["vault_a"]}]
    with pytest.raises(StorageTargetError, match="vault_a"):
        _write(root, config=cfg, targets=("vault_a",))
    assert not (root / "vault_a" / f"{UID}.md").exists()
