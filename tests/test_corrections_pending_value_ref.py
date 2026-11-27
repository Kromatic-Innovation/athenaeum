# SPDX-License-Identifier: Apache-2.0
"""A vault field's value stays out of the correction question block (athenaeum#3180)."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest

from athenaeum import corrections, pii
from athenaeum.answers import ingest_answers, parse_pending_questions
from athenaeum.decisions import list_pending_decisions
from athenaeum.librarian import _run_correction_phase
from athenaeum.models import parse_frontmatter
from tests.test_librarian_corrections import _batch, _make_ctx

UID = "person-a"
OFF = {"embedded": False, "recallable": False, "merge_eligible": False}
VALUE = "zz-value-sentinel-zz"
HANDLE = "zz-handle-sentinel@example.org"
SUBMITTER = "enrichment-service"


def _config(routes: bool = True) -> dict[str, Any]:
    cfg: dict[str, Any] = {
        "librarian": {
            "corrections": {
                "fields": {"current_title": {"shape": "scalar", "writers": [SUBMITTER]}}
            }
        },
        "storage": {
            "adapters": {
                "vault": {
                    "backing_store": "markdown",
                    "surface_root": "vault",
                    "corpus_policy": OFF,
                    "sensitive": True,
                }
            },
            "mapping": {"pii": "vault"},
        },
    }
    if routes:
        cfg["storage"]["routes"] = [
            {"page_type": "person", "fields": ["current_title", "emails"], "to": ["vault"]}
        ]
    return cfg


def _seed(tmp_path: Path, *, routed: bool = True) -> None:
    wiki = tmp_path / "wiki"
    wiki.mkdir(parents=True)
    # An undated tie against an api: incumbent is escalated by the precedence rules.
    incumbent = "field_sources:\n  current_title: 'api:apollo'\n"
    page = f"---\nuid: {UID}\ntype: person\nname: A\n"
    if routed:
        (tmp_path / "vault").mkdir()
        (tmp_path / "vault" / f"{UID}.md").write_text(
            f"---\nuid: {UID}\nemails:\n  - {HANDLE}\ncurrent_title: CTO\n{incumbent}---\n",
            encoding="utf-8",
        )
        page += "---\nBody.\n"
    else:
        page += f"current_title: CTO\n{incumbent}---\nBody.\n"
    (wiki / "a.md").write_text(page, encoding="utf-8")


def _batch_file(tmp_path: Path, target: dict[str, Any], value: str = VALUE) -> None:
    raw = tmp_path / "raw" / SUBMITTER
    raw.mkdir(parents=True, exist_ok=True)
    (raw / "20260806T030000Z-1a2b3c4d.jsonl").write_text(
        _batch(
            {
                "record": "correction",
                "target": target,
                "op": "set",
                "field": "current_title",
                "value": value,
                "source": "api:enrichment-vendor",
                "observed_at": "2026-08-06T05:58:40Z",
            },
            submitter=SUBMITTER,
        )
    )


def _escalate(tmp_path: Path, config: dict[str, Any]) -> str:
    _run_correction_phase(_make_ctx(tmp_path, config))
    return (tmp_path / "wiki" / "_pending_questions.md").read_text(encoding="utf-8")


def _entries(tmp_path: Path) -> dict[str, Any]:
    meta, _ = parse_frontmatter((tmp_path / "vault" / f"{UID}.md").read_text(encoding="utf-8"))
    return (meta.get("sections") or {}).get("pending_corrections", {})


def _tree_text(root: Path) -> str:
    """The pending file, its archive and every answer file; not the submitter's own batch."""
    files = [
        root / "wiki" / "_pending_questions.md",
        root / "wiki" / "_pending_questions_archive.md",
        *sorted((root / "raw" / "answers").glob("*")),
        *sorted((root / "wiki").glob("_decisions_queue*")),
        *sorted((root / "wiki").glob("_presentations*")),
    ]
    return "\n".join(p.read_text(encoding="utf-8") for p in files if p.is_file())


def _answer(tmp_path: Path, config: dict[str, Any], token: str) -> int:
    pending = tmp_path / "wiki" / "_pending_questions.md"
    pending.write_text(
        pending.read_text(encoding="utf-8").replace("- [ ]", "- [x]", 1) + f"\n{token}\n",
        encoding="utf-8",
    )
    return ingest_answers(pending, tmp_path / "raw", config=config)


def test_block_carries_a_reference_and_the_value_is_on_the_vault_record(tmp_path: Path) -> None:
    _seed(tmp_path)
    _batch_file(tmp_path, {"uid": UID})
    text = _escalate(tmp_path, _config())
    assert VALUE not in text
    entries = _entries(tmp_path)
    (ref,) = entries
    assert f"Value: vault:{ref}" in text and f"Correction ID: {ref}" in text
    assert len(ref) == 32
    entry = entries[ref]
    assert entry["value"] == VALUE and entry["field"] == "current_title"
    assert entry["op"] == "set" and entry["source"] == "api:enrichment-vendor"
    assert entry["observed_at"] == "2026-08-06T05:58:40Z"
    # Not derived from the value: the id the block used to carry is absent.
    old_id = corrections.compute_correction_id(
        schema_version=1, target={"uid": UID}, op="set", field_name="current_title", value=VALUE
    )
    assert old_id not in text


def test_a_second_pass_adds_no_block(tmp_path: Path) -> None:
    _seed(tmp_path)
    _batch_file(tmp_path, {"uid": UID})
    first = _escalate(tmp_path, _config())
    # The batch is carried over or retired; put it back and run again.
    _batch_file(tmp_path, {"uid": UID})
    second = _escalate(tmp_path, _config())
    assert first == second
    assert len(_entries(tmp_path)) == 1
    open_ids = corrections.open_correction_ids(
        parse_pending_questions(tmp_path / "wiki" / "_pending_questions.md")
    )
    assert set(_entries(tmp_path)) <= open_ids


def test_no_rule_gives_the_block_it_always_did(tmp_path: Path) -> None:
    _seed(tmp_path, routed=False)
    _batch_file(tmp_path, {"uid": UID})
    text = _escalate(tmp_path, _config(routes=False))
    old_id = corrections.compute_correction_id(
        schema_version=1, target={"uid": UID}, op="set", field_name="current_title", value=VALUE
    )
    assert f"Value: {VALUE!r}" in text and f"Correction ID: {old_id}" in text
    assert "vault:" not in text
    assert "sections" not in (tmp_path / "wiki" / "a.md").read_text(encoding="utf-8")


def test_sentinel_value_and_handle_leave_no_trace(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    _seed(tmp_path)
    _batch_file(tmp_path, {"type": "person", "handle": {"email": HANDLE}})
    cfg = _config()
    text = _escalate(tmp_path, cfg)
    assert f'"uid": "{UID}"' in text and "Target:" in text  # printed as the uid
    ids = {
        corrections.compute_correction_id(
            schema_version=1, target=target, op="set", field_name="current_title", value=VALUE
        )
        for target in ({"type": "person", "handle": {"email": HANDLE}}, {"uid": UID})
    }
    assert _answer(tmp_path, cfg, "reject") == 1
    everything = _tree_text(tmp_path)
    for needle in (VALUE, HANDLE, *ids):
        assert needle not in everything
        assert needle not in caplog.text


def test_listing_redacts_by_caller_and_shows_the_value_to_an_allowed_one(tmp_path: Path) -> None:
    _seed(tmp_path)
    _batch_file(tmp_path, {"uid": UID})
    _escalate(tmp_path, _config())
    (tmp_path / "athenaeum.yaml").write_text(
        json.dumps(_config()), encoding="utf-8"
    )  # YAML is a superset of JSON
    wiki = tmp_path / "wiki"
    hidden = json.dumps(list_pending_decisions(wiki))
    assert VALUE not in hidden and "redacted: current_title" in hidden
    shown = json.dumps(list_pending_decisions(wiki, include_sensitive=True))
    assert VALUE in shown
    restricted = json.dumps(
        list_pending_decisions(wiki, include_sensitive=True, caller_audience={"someone"})
    )
    assert VALUE not in restricted
    # The stored queue only ever holds the reference.
    assert VALUE not in _tree_text(tmp_path)


def test_ratify_applies_the_stored_value_like_a_clear_block_and_drops_the_entry(
    tmp_path: Path,
) -> None:
    _seed(tmp_path)
    _batch_file(tmp_path, {"uid": UID})
    cfg = _config()
    _escalate(tmp_path, cfg)
    assert _answer(tmp_path, cfg, "ratify") == 1
    meta, _ = parse_frontmatter((tmp_path / "vault" / f"{UID}.md").read_text(encoding="utf-8"))
    assert meta["current_title"] == VALUE
    assert meta["field_sources"]["current_title"].startswith("user:pending-question:")
    assert "sections" not in meta  # the pending entry is removed

    # Regression: the same value, no rule, so the block carries it in clear as it
    # always has; ratifying it gives the same result for the field.
    clear = tmp_path / "clear"
    clear.mkdir()
    _seed(clear, routed=False)
    _batch_file(clear, {"uid": UID})
    plain = _config(routes=False)
    assert f"Value: {VALUE!r}" in _escalate(clear, plain)
    assert _answer(clear, plain, "ratify") == 1
    cmeta, _ = parse_frontmatter((clear / "wiki" / "a.md").read_text(encoding="utf-8"))
    assert cmeta["current_title"] == meta["current_title"] == VALUE
    assert cmeta["field_sources"]["current_title"].startswith("user:pending-question:")


def test_reject_drops_the_pending_entry(tmp_path: Path) -> None:
    _seed(tmp_path)
    _batch_file(tmp_path, {"uid": UID})
    cfg = _config()
    _escalate(tmp_path, cfg)
    assert _answer(tmp_path, cfg, "reject") == 1
    assert _entries(tmp_path) == {}
    meta, _ = parse_frontmatter((tmp_path / "vault" / f"{UID}.md").read_text(encoding="utf-8"))
    assert meta["current_title"] == "CTO"


def test_an_unreadable_value_holds_the_answer_and_writes_nothing(tmp_path: Path) -> None:
    _seed(tmp_path)
    _batch_file(tmp_path, {"uid": UID})
    cfg = _config()
    _escalate(tmp_path, cfg)
    vault_file = tmp_path / "vault" / f"{UID}.md"
    before = vault_file.read_text(encoding="utf-8")
    # Drop the pending entry: the value can no longer be read.
    (ref,) = _entries(tmp_path)
    pii.delete_record_section(tmp_path, cfg, UID, "pending_corrections", ref, targets=("vault",))
    kept = vault_file.read_text(encoding="utf-8")
    assert kept != before
    _answer(tmp_path, cfg, "ratify")
    assert vault_file.read_text(encoding="utf-8") == kept
    assert "held" in (tmp_path / "wiki" / "_pending_questions.md").read_text(encoding="utf-8")


def test_a_block_edited_to_point_at_another_reference_is_held(tmp_path: Path) -> None:
    _seed(tmp_path)
    _batch_file(tmp_path, {"uid": UID})
    cfg = _config()
    text = _escalate(tmp_path, cfg)
    (ref,) = _entries(tmp_path)
    other = "0" * 32
    desc = text.split("**Description**:", 1)[1]
    # Value and id both edited to a reference no record carries: held.
    edited = desc.replace(ref, other)
    assert (
        corrections.resolve_pending_value(
            corrections.parse_escalated_correction(edited),
            tmp_path,
            cfg,  # type: ignore[arg-type]
        )
        == "pending value could not be read: no entry for the block's reference"
    )
    # Only the id edited: the integrity check refuses it at parse time.
    half = desc.replace(f"Correction ID: {ref}", f"Correction ID: {other}")
    assert corrections.parse_escalated_correction(half) == (
        "block fields do not match its Correction ID"
    )
    # Another record's reference on the same entity with a different field is held.
    pii.write_record_section(
        tmp_path,
        cfg,
        UID,
        "pending_corrections",
        other,
        {"field": "emails", "op": "add", "value": "x", "schema_version": 1},
        targets=("vault",),
    )
    parsed = corrections.parse_escalated_correction(edited)
    assert not isinstance(parsed, str)
    assert corrections.resolve_pending_value(parsed, tmp_path, cfg) == (
        "pending value does not belong to this block"
    )


def test_old_blocks_still_parse() -> None:
    cid = corrections.compute_correction_id(
        schema_version=1, target={"uid": UID}, op="set", field_name="f", value="v"
    )
    old = (
        f'Target: {{"uid": "{UID}"}}\nField: f\nOp: set\nValue: \'v\'\n'
        f"Source: s\nReason: r\nCorrection ID: {cid}"
    )
    parsed = corrections.parse_escalated_correction(old)
    assert not isinstance(parsed, str) and parsed.value == "v" and parsed.value_ref is None


def test_a_failed_escalation_leaves_no_pending_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import athenaeum.librarian as librarian_mod

    def boom(*args: Any, **kwargs: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(librarian_mod, "tier4_escalate", boom)
    _seed(tmp_path)
    _batch_file(tmp_path, {"uid": UID})
    with pytest.raises(OSError, match="disk full"):
        _escalate(tmp_path, _config())
    assert _entries(tmp_path) == {}
