"""Corrections route by the storage resolver; the old table is refused (athenaeum#3162)."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml

from athenaeum import pii
from athenaeum.config import _REMOVED_CONFIG_KEYS, find_removed_config_keys
from athenaeum.corrections import (
    RemovedCorrectionsConfigError,
    process_correction_record,
    refuse_removed_sensitive_fields,
    removed_sensitive_fields_message,
    run_correction_phase,
    run_trust_dryrun,
)
from athenaeum.models import EntityIndex, parse_frontmatter

# athenaeum#3170: nothing is private unless declared; these tests declare the contact fields.
_CF = ["emails", "phones", "former_emails", "alt_emails"]

UID = "person-alex"
OLD_KEY = "librarian.corrections.sensitive_fields"


def _root(tmp_path: Path) -> Path:
    wiki = tmp_path / "wiki"
    wiki.mkdir(parents=True)
    (wiki / "alex.md").write_text(
        f"---\nuid: {UID}\ntype: person\nname: Alex Doe\n---\nBody.\n", encoding="utf-8"
    )
    (tmp_path / "excluded").mkdir()
    (tmp_path / "excluded" / f"{UID}.md").write_text(
        f"---\nuid: {UID}\nemails:\n  - alex@example.org\n---\n", encoding="utf-8"
    )
    return tmp_path


def _fields() -> dict[str, Any]:
    return {
        "bounced": {"shape": "scalar", "writers": ["delivery-monitor"]},
        "emails": {"shape": "list", "writers": ["delivery-monitor"]},
    }


def _record(field: str, op: str, value: str) -> dict[str, Any]:
    return {
        "record": "correction",
        "target": {"uid": UID},
        "op": op,
        "field": field,
        "value": value,
        "source": "api:delivery-monitor:2026-08-06",
        "observed_at": "2026-08-06T14:01:55Z",
    }


def _apply(root: Path, cfg: dict[str, Any], record: dict[str, Any]) -> Any:
    envelope = {
        "record": "batch",
        "schema_version": 1,
        "submitter": "delivery-monitor",
        "batch_id": "20260806T140211Z-9f3ac1d2",
        "created_at": "2026-08-06T14:02:11Z",
        "defaults": {},
    }
    registry = {UID: {"type": "person", "handles": {"emails": ["alex@example.org"]}}}
    return process_correction_record(
        record,
        envelope,
        index=EntityIndex(root / "wiki"),
        knowledge_root=root,
        registry_entities=registry,
        config=cfg,
    )


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*.md"))
    }


def _old_config() -> dict[str, Any]:
    return {
        "librarian": {
            "corrections": {"fields": _fields(), "sensitive_fields": {"bounced": "pii"}}
        },
        "storage": {"mapping": {"pii": "excluded"}, "excluded_fields": {"pii": _CF}},
    }


def _pasted_config() -> dict[str, Any]:
    """The old configuration with the refusal's printed text applied to it."""
    old = _old_config()
    message = removed_sensitive_fields_message(old)
    assert message is not None
    lines = message.splitlines()
    start = lines.index("storage:")
    end = lines.index("", start)
    pasted = yaml.safe_load("\n".join(lines[start:end]))
    cfg = copy.deepcopy(old)
    del cfg["librarian"]["corrections"]["sensitive_fields"]
    cfg["storage"].update(pasted["storage"])
    return cfg


def test_printed_replacement_includes_builtin_contact_fields() -> None:
    cfg = _pasted_config()
    listed = cfg["storage"]["excluded_fields"]["pii"]
    assert listed == [*pii.BUILTIN_CONTACT_FIELDS, "bounced"]


def test_printed_replacement_reproduces_the_old_routing_byte_for_byte(tmp_path: Path) -> None:
    # The old key's behaviour, spelled out: a routed field lands on the
    # surface record and leaves the page untouched.
    ref = _root(tmp_path / "ref")
    page_before = (ref / "wiki" / "alex.md").read_bytes()

    hand = _root(tmp_path / "hand")
    hand_cfg = {
        "librarian": {"corrections": {"fields": _fields()}},
        "storage": {
            "mapping": {"pii": "excluded"},
            "excluded_fields": {
                "pii": ["emails", "phones", "former_emails", "alt_emails", "bounced"]
            },
        },
    }
    pasted = _root(tmp_path / "pasted")
    rec = _record("bounced", "set", "2026-08-06")
    r_hand = _apply(hand, hand_cfg, rec)
    r_pasted = _apply(pasted, _pasted_config(), rec)
    assert r_hand.disposition == r_pasted.disposition == "routed-elsewhere"
    # Same bytes in both trees, and the page is untouched, as under the old key.
    assert _snapshot(hand) == _snapshot(pasted)
    assert (pasted / "wiki" / "alex.md").read_bytes() == page_before
    record, _ = parse_frontmatter((pasted / "excluded" / f"{UID}.md").read_text(encoding="utf-8"))
    assert record["bounced"] == "2026-08-06"


def test_default_routes_emails_to_the_separate_location(tmp_path: Path) -> None:
    # No corrections table at all: contact fields are kept on the excluded
    # location for every writer, so a correction to `emails` goes there.
    root = _root(tmp_path)
    page = root / "wiki" / "alex.md"
    before = page.read_bytes()
    cfg = {
        "librarian": {"corrections": {"fields": _fields()}},
        "storage": {"mapping": {"pii": "excluded"}, "excluded_fields": {"pii": _CF}},
    }
    result = _apply(root, cfg, _record("emails", "add", "alex2@example.org"))
    assert result.disposition == "routed-elsewhere"
    assert page.read_bytes() == before
    record, _ = parse_frontmatter((root / "excluded" / f"{UID}.md").read_text(encoding="utf-8"))
    assert "alex2@example.org" in record["emails"]


def test_no_separate_location_writes_in_place(tmp_path: Path) -> None:
    root = _root(tmp_path)
    cfg = {"librarian": {"corrections": {"fields": _fields()}}}
    result = _apply(root, cfg, _record("emails", "add", "alex2@example.org"))
    assert result.disposition == "applied"
    meta, _ = parse_frontmatter((root / "wiki" / "alex.md").read_text(encoding="utf-8"))
    assert "alex2@example.org" in meta["emails"]


def test_removed_key_is_registered() -> None:
    assert OLD_KEY in _REMOVED_CONFIG_KEYS
    assert [k for k, _ in find_removed_config_keys(_old_config())] == [OLD_KEY]


def test_empty_removed_key_is_not_refused() -> None:
    cfg = {"librarian": {"corrections": {"sensitive_fields": {}}}}
    refuse_removed_sensitive_fields(cfg)
    assert [k for k, _ in find_removed_config_keys(cfg)] == [OLD_KEY]


def _batch_root(tmp_path: Path) -> Path:
    root = _root(tmp_path)
    src = root / "raw" / "delivery-monitor"
    src.mkdir(parents=True)
    envelope = (
        '{"record": "batch", "schema_version": 1, "submitter": "delivery-monitor", '
        '"batch_id": "20260806T140211Z-9f3ac1d2", "created_at": "2026-08-06T14:02:11Z", '
        '"defaults": {}}'
    )
    rec = (
        '{"record": "correction", "target": {"uid": "person-alex"}, "op": "set", '
        '"field": "bounced", "value": "2026-08-06", '
        '"source": "api:delivery-monitor:2026-08-06", "observed_at": "2026-08-06T14:01:55Z"}'
    )
    (src / "20260806T140211Z-9f3ac1d2.jsonl").write_text(envelope + "\n" + rec + "\n")
    return root


def test_run_refuses_before_any_batch_is_read(tmp_path: Path) -> None:
    root = _batch_root(tmp_path)
    before = _snapshot(root)
    batch = root / "raw" / "delivery-monitor" / "20260806T140211Z-9f3ac1d2.jsonl"
    batch_before = batch.read_bytes()
    with pytest.raises(RemovedCorrectionsConfigError) as exc:
        run_correction_phase(
            raw_root=root / "raw",
            wiki_root=root / "wiki",
            knowledge_root=root,
            index=EntityIndex(root / "wiki"),
            config=_old_config(),
            escalate_one=lambda _r, _o: False,
        )
    text = str(exc.value)
    assert "emails" in text and "alt_emails" in text and "bounced" in text
    assert _snapshot(root) == before
    assert batch.read_bytes() == batch_before
    assert not (root / "raw" / "delivery-monitor" / "applied").exists()


def test_trust_dryrun_refuses_too(tmp_path: Path) -> None:
    root = _batch_root(tmp_path)
    with pytest.raises(RemovedCorrectionsConfigError):
        run_trust_dryrun(
            raw_root=root / "raw", wiki_root=root / "wiki", knowledge_root=root,
            config=_old_config(),
        )


def test_run_with_replacement_config_applies(tmp_path: Path) -> None:
    root = _batch_root(tmp_path)
    summary = run_correction_phase(
        raw_root=root / "raw",
        wiki_root=root / "wiki",
        knowledge_root=root,
        index=EntityIndex(root / "wiki"),
        config=_pasted_config(),
        escalate_one=lambda _r, _o: False,
    )
    assert summary["dispositions"].get("routed-elsewhere") == 1


def test_non_correction_command_shows_notice_and_keeps_running(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from athenaeum.config import format_removed_key_notice, removed_config_keys_in_file

    (tmp_path / "athenaeum.yaml").write_text(yaml.safe_dump(_old_config()), encoding="utf-8")
    found = removed_config_keys_in_file(tmp_path)
    assert [k for k, _ in found] == [OLD_KEY]
    assert "athenaeum#3162" in format_removed_key_notice(*found[0])
