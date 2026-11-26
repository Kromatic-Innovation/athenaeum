"""A ``storage.routes`` rule with several vault targets (athenaeum#3169)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from athenaeum import corrections as corrections_mod
from athenaeum import doctor
from athenaeum.corrections import process_correction_record
from athenaeum.models import EntityIndex, parse_frontmatter
from athenaeum.retire_targets import RetireRunReport, _erase_secondary_targets
from athenaeum.sensitivity_routing import SensitivityRoutingError, _route_one
from athenaeum.status import format_status, status
from athenaeum.storage import (
    StorageConfigError,
    StorageTargetError,
    configured_routes,
    resolve_adapter_for_class,
    route_for,
    route_target_summary,
)

UID = "person-alex"
OFF = {"embedded": False, "recallable": False, "merge_eligible": False}
ON = {"embedded": True, "recallable": True, "merge_eligible": True}


def _adapters(tmp_path: Path) -> dict[str, Any]:
    def vault(root: str, **kw: Any) -> dict[str, Any]:
        return {
            "backing_store": "markdown",
            "surface_root": root,
            "corpus_policy": OFF,
            "sensitive": True,
            **kw,
        }

    return {
        "vault_a": vault("vault_a"),
        "vault_b": vault("vault_b"),
        # Its own git repository, outside the knowledge root's tree.
        "vault_git": vault(str(tmp_path / "gitrepo" / "vault")),
        "vault_ro": vault("vault_ro", read_only=True),
        "archive": {"backing_store": "markdown", "surface_root": "archive", "corpus_policy": OFF},
        "shared": {"backing_store": "markdown", "surface_root": "shared", "corpus_policy": ON},
    }


def _cfg(tmp_path: Path, routes: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "librarian": {
            "corrections": {
                "fields": {
                    "emails": {"shape": "list", "writers": ["intake-bot"]},
                    "ssn": {"shape": "scalar", "writers": ["intake-bot"]},
                }
            }
        },
        "storage": {"adapters": _adapters(tmp_path), "routes": routes},
    }


FIELD_RULE = {"page_type": "person", "fields": ["emails", "ssn"], "to": ["vault_a", "vault_b"]}


def _knowledge(tmp_path: Path) -> Path:
    wiki = tmp_path / "wiki"
    wiki.mkdir(parents=True)
    (wiki / "alex.md").write_text(
        f"---\nuid: {UID}\ntype: person\nname: Alex Doe\n---\nBody.\n", encoding="utf-8"
    )
    return tmp_path


def _apply(
    root: Path, cfg: dict[str, Any], op: str, field: str, value: str, **extra: Any
) -> Any:
    envelope = {
        "record": "batch",
        "schema_version": 1,
        "submitter": "intake-bot",
        "batch_id": "20260806T140211Z-9f3ac1d2",
        "created_at": "2026-08-06T14:02:11Z",
        "defaults": {},
    }
    record = {
        "record": "correction",
        "target": {"uid": UID},
        "op": op,
        "field": field,
        "value": value,
        "source": "api:intake-bot:2026-08-06",
        "observed_at": "2026-08-06T14:01:55Z",
        **extra,
    }
    return process_correction_record(
        record,
        envelope,
        index=EntityIndex(root / "wiki"),
        knowledge_root=root,
        registry_entities={UID: {"type": "person", "handles": {}}},
        config=cfg,
    )


def _meta(root: Path, vault: str) -> dict[str, Any]:
    return parse_frontmatter((root / vault / f"{UID}.md").read_text(encoding="utf-8"))[0]


# --- AC1: validity and order -----------------------------------------------------


def test_several_vaults_are_valid_and_keep_their_order(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, [FIELD_RULE])
    assert configured_routes(cfg)[0].to == ("vault_a", "vault_b")
    decision = route_for("person", "emails", config=cfg)
    assert decision.targets == ("vault_a", "vault_b") and decision.sensitive
    reversed_cfg = _cfg(tmp_path, [{**FIELD_RULE, "to": ["vault_b", "vault_a"]}])
    assert route_for("person", "emails", config=reversed_cfg).targets == ("vault_b", "vault_a")
    # The first is the primary: the one reads resolve from.
    assert resolve_adapter_for_class("routes.person", reversed_cfg).name == "vault_b"


def test_whole_page_type_and_class_rules_take_several_vaults(tmp_path: Path) -> None:
    cfg = _cfg(
        tmp_path,
        [
            {"page_type": "diary", "to": ["vault_a", "vault_b"]},
            {"sensitivity_class": "secret", "to": ["vault_b", "vault_a"]},
        ],
    )
    assert route_for("diary", config=cfg).targets == ("vault_a", "vault_b")
    assert len(configured_routes(cfg)) == 2


@pytest.mark.parametrize("bad", ["shared", "archive"])
def test_a_list_with_a_location_that_is_not_a_vault_is_refused(tmp_path: Path, bad: str) -> None:
    cfg = _cfg(tmp_path, [{**FIELD_RULE, "to": ["vault_a", bad]}])
    with pytest.raises(StorageConfigError, match=bad):
        configured_routes(cfg)


def test_a_list_including_an_indexed_location_is_refused_naming_the_index(
    tmp_path: Path,
) -> None:
    cfg = _cfg(tmp_path, [{"page_type": "diary", "to": ["shared", "vault_a"]}])
    with pytest.raises(StorageConfigError, match="in the index"):
        configured_routes(cfg)


def test_a_repeated_target_is_refused(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, [{**FIELD_RULE, "to": ["vault_a", "vault_a"]}])
    with pytest.raises(StorageConfigError, match="more than once"):
        configured_routes(cfg)


# --- AC6: read-only target -------------------------------------------------------


def test_a_read_only_target_in_a_list_raises_storage_config_error(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, [{**FIELD_RULE, "to": ["vault_a", "vault_ro"]}])
    with pytest.raises(StorageConfigError, match="read_only"):
        configured_routes(cfg)


# --- AC2/AC3: a write goes to every target, idempotently ---------------------------


def test_a_correction_is_written_to_every_target_in_order(tmp_path: Path) -> None:
    root = _knowledge(tmp_path)
    cfg = _cfg(tmp_path, [FIELD_RULE])
    result = _apply(root, cfg, "add", "emails", "alex@example.org")
    assert result.disposition == "routed-elsewhere"
    for vault in ("vault_a", "vault_b"):
        assert _meta(root, vault)["emails"] == ["alex@example.org"]
    assert "emails" not in parse_frontmatter((root / "wiki" / "alex.md").read_text())[0]


def test_a_failing_target_raises_naming_it_and_a_rerun_completes_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _knowledge(tmp_path)
    cfg = _cfg(tmp_path, [FIELD_RULE])
    real = corrections_mod._write_surface_record
    failures = {"left": 1}

    def flaky(path: Path, record: dict[str, Any], *, body: str = "") -> None:
        if "vault_b" in path.parts and failures["left"]:
            failures["left"] -= 1
            raise OSError("disk full")
        real(path, record, body=body)

    monkeypatch.setattr(corrections_mod, "_write_surface_record", flaky)
    with pytest.raises(StorageTargetError, match="vault_b") as err:
        _apply(root, cfg, "add", "emails", "alex@example.org")
    assert err.value.target == "vault_b"
    assert _meta(root, "vault_a")["emails"] == ["alex@example.org"]
    assert not (root / "vault_b" / f"{UID}.md").exists()

    # The same correction again: the primary already holds it, the rest is completed.
    _apply(root, cfg, "add", "emails", "alex@example.org")
    assert _meta(root, "vault_a")["emails"] == _meta(root, "vault_b")["emails"]
    assert _meta(root, "vault_b")["emails"] == ["alex@example.org"]
    # And once more: nothing is duplicated on any target.
    _apply(root, cfg, "add", "emails", "alex@example.org")
    assert _meta(root, "vault_a")["emails"] == _meta(root, "vault_b")["emails"] == [
        "alex@example.org"
    ]


def test_healing_a_later_target_still_classifies_the_primary(tmp_path: Path) -> None:
    root = _knowledge(tmp_path)
    cfg = _cfg(tmp_path, [FIELD_RULE])
    _apply(root, cfg, "add", "emails", "alex@example.org")
    # Out-of-band: the later target lost the record, and neither is classified.
    (root / "vault_b" / f"{UID}.md").unlink()
    assert "contact_classification" not in _meta(root, "vault_a")

    result = _apply(root, cfg, "add", "emails", "alex@example.org", usage_class="observed")
    assert result.disposition == "routed-elsewhere"
    for vault in ("vault_a", "vault_b"):
        entries = _meta(root, vault).get("contact_classification") or []
        assert [e["usage_class"] for e in entries] == ["observed"], vault


def test_a_scalar_set_reaches_every_target(tmp_path: Path) -> None:
    root = _knowledge(tmp_path)
    _apply(root, _cfg(tmp_path, [FIELD_RULE]), "set", "ssn", "123")
    assert _meta(root, "vault_a")["ssn"] == _meta(root, "vault_b")["ssn"] == "123"


def test_a_routed_free_text_value_is_written_to_each_vault_and_rerun_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _cfg(tmp_path, [{"sensitivity_class": "secret", "to": ["vault_a", "vault_b"]}])
    kwargs: dict[str, Any] = dict(
        raw_ref="raw/a.md",
        sensitivity_class="secret",
        start=0,
        end=6,
        value="hunter",
        config=cfg,
        knowledge_root=tmp_path,
    )
    import athenaeum.sensitivity_routing as sr

    real = sr.atomic_write_text
    failures = {"left": 1}

    def flaky(path: Path, text: str) -> None:
        if "vault_b" in path.parts and failures["left"]:
            failures["left"] -= 1
            raise OSError("unreachable")
        real(path, text)

    monkeypatch.setattr(sr, "atomic_write_text", flaky)
    with pytest.raises(SensitivityRoutingError, match="vault_b"):
        _route_one(**kwargs)
    pointer = _route_one(**kwargs)
    a = sorted((tmp_path / "vault_a" / "sensitivity" / "secret").glob("*.md"))
    b = sorted((tmp_path / "vault_b" / "sensitivity" / "secret").glob("*.md"))
    assert [p.name for p in a] == [p.name for p in b] and len(a) == 1
    assert a[0].read_text() == b[0].read_text()
    assert a[0].stem in pointer


# --- AC4: reads from the primary; a divergence is reported -------------------------


def test_doctor_reports_a_value_on_a_later_target_missing_from_the_primary(
    tmp_path: Path,
) -> None:
    root = _knowledge(tmp_path)
    cfg = _cfg(tmp_path, [FIELD_RULE])
    _apply(root, cfg, "add", "emails", "alex@example.org")
    assert doctor.check_multi_target_divergence(cfg, root).status == "ok"

    # An out-of-band edit leaves the value only on the later target.
    (root / "vault_a" / f"{UID}.md").write_text(f"---\nuid: {UID}\nemails: []\n---\n")
    result = doctor.check_multi_target_divergence(cfg, root)
    assert result.status == "fail"
    assert "vault_b" in result.detail and "emails" in result.detail
    assert "alex@example.org" not in result.detail

    # A record only on the later target is reported too, and is not served.
    (root / "vault_a" / f"{UID}.md").unlink()
    assert "not on the primary" in doctor.check_multi_target_divergence(cfg, root).detail


def test_doctor_divergence_is_ok_without_a_multi_target_rule(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, [{**FIELD_RULE, "to": ["vault_a"]}])
    assert doctor.check_multi_target_divergence(cfg, tmp_path).status == "ok"


# --- AC5: removal and erasure reach every target ------------------------------------


def test_removing_a_value_applies_to_every_target(tmp_path: Path) -> None:
    root = _knowledge(tmp_path)
    cfg = _cfg(tmp_path, [FIELD_RULE])
    _apply(root, cfg, "add", "emails", "alex@example.org")
    _apply(root, cfg, "remove", "emails", "alex@example.org")
    for vault in ("vault_a", "vault_b"):
        assert _meta(root, vault)["emails"] == []


def test_removal_names_an_unreachable_target(tmp_path: Path) -> None:
    root = _knowledge(tmp_path)
    cfg = _cfg(tmp_path, [FIELD_RULE])
    _apply(root, cfg, "add", "emails", "alex@example.org")
    import shutil

    shutil.rmtree(root / "vault_b")
    with pytest.raises(StorageTargetError, match="vault_b"):
        _apply(root, cfg, "remove", "emails", "alex@example.org")


def test_erasing_an_entity_removes_its_record_from_every_later_target(tmp_path: Path) -> None:
    root = _knowledge(tmp_path)
    cfg = _cfg(tmp_path, [FIELD_RULE])
    _apply(root, cfg, "add", "emails", "alex@example.org")
    other = root / "vault_b" / "person-other.md"
    other.write_text("---\nuid: person-other\n---\n")
    report = RetireRunReport()
    _erase_secondary_targets(root, {UID}, cfg, report)
    assert report.errors == [] and report.secondary_erased == 1
    assert not (root / "vault_b" / f"{UID}.md").exists()
    assert other.exists()


def test_erasure_treats_a_missing_target_root_as_empty(tmp_path: Path) -> None:
    root = _knowledge(tmp_path)
    cfg = _cfg(tmp_path, [{**FIELD_RULE, "to": ["vault_a", "vault_b", "vault_git"]}])
    _apply(root, cfg, "add", "emails", "alex@example.org")
    import shutil

    shutil.rmtree(root / "vault_b")
    report = RetireRunReport()
    _erase_secondary_targets(root, {UID}, cfg, report)
    assert report.errors == []
    assert not (tmp_path / "gitrepo" / "vault" / f"{UID}.md").exists()


def test_erasure_names_an_unreachable_target_and_still_reaches_the_rest(
    tmp_path: Path,
) -> None:
    root = _knowledge(tmp_path)
    cfg = _cfg(tmp_path, [{**FIELD_RULE, "to": ["vault_a", "vault_b", "vault_git"]}])
    _apply(root, cfg, "add", "emails", "alex@example.org")
    import shutil

    # A file where the directory should be: exists, but cannot be listed.
    shutil.rmtree(root / "vault_b")
    (root / "vault_b").write_text("not a directory")
    report = RetireRunReport()
    _erase_secondary_targets(root, {UID}, cfg, report)
    assert len(report.errors) == 1 and "vault_b" in report.errors[0]
    assert not (tmp_path / "gitrepo" / "vault" / f"{UID}.md").exists()


def test_a_secondary_target_without_compile_refuses_the_run_before_any_delete(
    tmp_path: Path,
) -> None:
    from athenaeum.retire_targets import RetirePlan, RetireTarget, apply_plan

    root = _knowledge(tmp_path)
    cfg = _cfg(tmp_path, [{**FIELD_RULE, "to": ["vault_a", "vault_b", "vault_git"]}])
    _apply(root, cfg, "add", "emails", "alex@example.org")
    # A declaration that is present but invalid resolves as reader only.
    (root / "vault_b" / "_store").mkdir()
    (root / "vault_b" / "_store" / "access.yaml").write_text("version: 99\n", encoding="utf-8")
    from athenaeum.retire_targets import target_access

    verdict, _ = target_access(root / "vault_b", root, cfg)
    assert verdict == "refused"

    plan = RetirePlan(targets=[RetireTarget(uid=UID, store="none", paths=[])])
    report = apply_plan(root, plan, reason="test", cache_dir=tmp_path / "cache", config=cfg)
    assert any("vault_b" in e for e in report.errors)
    assert report.secondary_erased == 0
    for path in (
        root / "vault_b" / f"{UID}.md",
        tmp_path / "gitrepo" / "vault" / f"{UID}.md",
    ):
        assert path.exists()

    erase = RetireRunReport()
    _erase_secondary_targets(root, {UID}, cfg, erase)
    assert any("vault_b" in e for e in erase.errors) and erase.secondary_erased == 0
    assert (tmp_path / "gitrepo" / "vault" / f"{UID}.md").exists()


def test_retire_command_exits_non_zero_when_a_target_is_unreachable(
    tmp_path: Path,
) -> None:
    from athenaeum.retire_targets import RetirePlan, RetireTarget, apply_plan

    root = _knowledge(tmp_path)
    cfg = _cfg(tmp_path, [FIELD_RULE])
    (root / "vault_a").mkdir()
    (root / "vault_b").write_text("not a directory")
    plan = RetirePlan(targets=[RetireTarget(uid=UID, store="none", paths=[])])
    report = apply_plan(root, plan, reason="test", cache_dir=tmp_path / "cache", config=cfg)
    # vault_b exists but is not a directory: named, and the command's error list is non-empty
    # (cmd_retire returns 1 for any entry in it).
    assert any("vault_b" in e for e in report.errors)


# --- AC7: status ----------------------------------------------------------------------


def test_status_counts_targets_per_rule_and_names_one_inside_git(tmp_path: Path) -> None:
    root = _knowledge(tmp_path)
    (tmp_path / "gitrepo" / ".git").mkdir(parents=True)
    cfg = _cfg(
        tmp_path,
        [
            {**FIELD_RULE, "to": ["vault_a", "vault_git"]},
            {"page_type": "diary", "to": ["vault_b"]},
            {"sensitivity_class": "secret", "to": []},
        ],
    )
    rows = route_target_summary(cfg, root)
    assert [r["target_count"] for r in rows] == [2, 1, 0]
    assert rows[0]["in_git"] == ["vault_git"] and rows[1]["in_git"] == []
    info = status(root)
    info["storage_routes"] = rows
    text = format_status(info)
    assert "storage.routes[0] (person): 2 targets" in text
    assert "storage.routes[1] (diary): 1 target " in text
    assert "'vault_git' is inside a git repository" in text
    assert text.count("is inside a git repository") == 1


# --- the contact-write mirrors (librarian correspondent write, prose scrub) ---------

CONTACT_RULE = {"page_type": "person", "fields": ["emails"], "to": ["vault_a", "vault_b"]}


def _contact_cfg(tmp_path: Path) -> dict[str, Any]:
    return {
        "storage": {
            "mapping": {"pii": "excluded"},
            "adapters": _adapters(tmp_path),
            "routes": [CONTACT_RULE],
        }
    }


def _listing(root: Path) -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in sorted(root.rglob("*.md")))


def test_librarian_correspondent_write_reaches_every_target_in_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import athenaeum.librarian as librarian
    from tests.test_correspondent_contact_record import _compile, _person, _prose_note

    real = librarian.record_observed_contact_value
    order: list[str] = []

    def spy(root: Path, *a: Any, **kw: Any) -> Any:
        order.append(root.name)
        return real(root, *a, **kw)

    monkeypatch.setattr(librarian, "record_observed_contact_value", spy)
    knowledge, result = _compile(
        tmp_path,
        monkeypatch,
        note=_prose_note(address="dana@example.org"),
        classified=[_person("Dana Quill")],
        config=_contact_cfg(tmp_path),
    )
    assert result.correspondent_contacts_recorded == 1  # type: ignore[attr-defined]
    assert order[-2:] == ["vault_a", "vault_b"]
    for vault in ("vault_a", "vault_b"):
        assert "dana@example.org" in _listing(knowledge / vault)


def test_librarian_correspondent_write_raises_naming_a_failing_later_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import athenaeum.librarian as librarian
    from tests.test_correspondent_contact_record import _compile, _person, _prose_note

    real = librarian.record_observed_contact_value

    def flaky(root: Path, *a: Any, **kw: Any) -> Any:
        if root.name == "vault_b":
            raise OSError("unreachable")
        return real(root, *a, **kw)

    monkeypatch.setattr(librarian, "record_observed_contact_value", flaky)
    with pytest.raises(StorageTargetError, match="vault_b"):
        _compile(
            tmp_path,
            monkeypatch,
            note=_prose_note(address="dana@example.org"),
            classified=[_person("Dana Quill")],
            config=_contact_cfg(tmp_path),
        )


def _enforced_context(tmp_path: Path) -> Any:
    import dataclasses

    from athenaeum import pii_attribution
    from athenaeum.pii_attribution import QUESTION_CLASSES

    knowledge = tmp_path / "knowledge"
    (knowledge / "wiki").mkdir(parents=True)
    context = pii_attribution.build_attribution_context(knowledge, _contact_cfg(tmp_path))
    return knowledge, dataclasses.replace(context, enforced_classes=frozenset(QUESTION_CLASSES))


def _scrub(context: Any) -> Any:
    from athenaeum import pii_attribution

    return pii_attribution.scrub_compiled_contact_tokens(
        "Write to dana@example.org soon.",
        subject_uid="uid-dana",
        source_ref="raw/x.md",
        context=context,
        target_name="Dana Quill",
        target_type="person",
    )


def test_prose_scrub_files_the_token_on_every_target_in_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from athenaeum import pii

    knowledge, context = _enforced_context(tmp_path)
    real = pii.record_contact_value
    order: list[str] = []

    def spy(root: Path, *a: Any, **kw: Any) -> Any:
        order.append(root.name)
        return real(root, *a, **kw)

    monkeypatch.setattr(pii, "record_contact_value", spy)
    scrubbed, removed = _scrub(context)
    assert removed == ["dana@example.org"] and "dana@example.org" not in scrubbed
    assert order[-2:] == ["vault_a", "vault_b"]
    for vault in ("vault_a", "vault_b"):
        assert "dana@example.org" in _listing(knowledge / vault)


def test_prose_scrub_raises_naming_a_failing_later_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from athenaeum import pii

    _knowledge_root, context = _enforced_context(tmp_path)
    real = pii.record_contact_value

    def flaky(root: Path, *a: Any, **kw: Any) -> Any:
        if root.name == "vault_b":
            raise OSError("unreachable")
        return real(root, *a, **kw)

    monkeypatch.setattr(pii, "record_contact_value", flaky)
    with pytest.raises(StorageTargetError, match="vault_b"):
        _scrub(context)


def test_batch_compile_routes_a_free_text_value_to_every_target(tmp_path: Path) -> None:
    from tests.test_storage_discard import SENTINEL_EMAIL, _batch_compile, _free_text_cfg

    cfg = _free_text_cfg([{"sensitivity_class": "pii", "to": ["vault_a", "vault_b"]}])
    cfg["storage"]["adapters"] = _adapters(tmp_path)
    raw_text = (
        "---\nuid: note-2\ntype: reference\nname: Mail note\naccess: internal\n---\n\n"
        f"Reach them at {SENTINEL_EMAIL} soon.\n"
    )
    knowledge = _batch_compile(tmp_path, cfg, raw_text)
    a = sorted((knowledge / "vault_a" / "sensitivity" / "pii").glob("*.md"))
    b = sorted((knowledge / "vault_b" / "sensitivity" / "pii").glob("*.md"))
    assert len(a) == 1 and [p.name for p in a] == [p.name for p in b]
    assert SENTINEL_EMAIL in a[0].read_text() and SENTINEL_EMAIL in b[0].read_text()
