"""A `storage.routes` rule with `to: []` discards what it names (athenaeum#3168)."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from athenaeum import pii
from athenaeum.corrections import (
    build_ledger_record,
    process_batch_file,
    process_correction_record,
    run_correction_phase,
)
from athenaeum.intake_decision import (
    CAPTURE_DROP,
    CAPTURE_KEEP,
    REASON_DISCARD,
    Description,
    decide,
)
from athenaeum.librarian import _render_run_summary, process_one
from athenaeum.models import EntityIndex, RawFile, parse_frontmatter
from athenaeum.relevance_policy import RelevancePolicy, gate_raw_content
from athenaeum.run_summary_log import build_run_summary_ledger_record
from athenaeum.sensitivity_routing import (
    SensitivityRoutingError,
    _render_vault_record,
    refuse_unrouted_value_classes,
    resolve_sensitive_record,
    route_sensitive_values,
)
from athenaeum.storage import (
    configured_routes,
    discard_counts,
    discarded_page_types,
    reset_discard_counts,
    route_for,
    route_for_sensitivity_class,
    routed_storage_mapping,
)
from tests.test_correspondent_contact_record import (
    EXCLUDED_CONFIG,
    _compile,
    _person,
    _prose_note,
)
from tests.test_librarian_run_phases import _make_ctx

SENTINEL = "zz-sentinel-7f3a9c"
SENTINEL_EMAIL = f"{SENTINEL}@example.invalid"
UID = "person-alex"


@pytest.fixture(autouse=True)
def _clean_counters(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATHENAEUM_CACHE_DIR", str(tmp_path / "cache"))
    reset_discard_counts()


def _cfg(routes: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    return {"storage": {"routes": routes}, **extra}


# --- AC1: `to: []` is valid, and is not "no rule" ------------------------------


def test_empty_to_is_valid_and_route_for_names_the_rule() -> None:
    cfg = _cfg([{"page_type": "person", "fields": ["ssn"], "to": []}])
    (rule,) = configured_routes(cfg)
    assert rule.discard and rule.to == ()
    decision = route_for("person", "ssn", config=cfg)
    assert decision.targets == ()
    assert decision.rule == "storage.routes[0]"
    assert decision.discarded


def test_discard_is_distinct_from_no_rule_matching() -> None:
    cfg = _cfg([{"page_type": "person", "fields": ["ssn"], "to": []}])
    unruled = route_for("person", "nickname", config=cfg)
    assert unruled.targets == () and unruled.rule is None
    assert not unruled.discarded
    assert unruled != route_for("person", "ssn", config=cfg)


def test_whole_page_type_and_class_rules_discard() -> None:
    cfg = _cfg(
        [
            {"page_type": "diary", "to": []},
            {"sensitivity_class": "secret", "to": []},
        ]
    )
    whole = route_for("diary", config=cfg)
    assert whole.discarded and whole.rule == "storage.routes[0]"
    # A field of a wholly discarded page type is discarded too.
    assert route_for("diary", "anything", config=cfg).discarded
    cls = route_for_sensitivity_class("secret", cfg)
    assert cls is not None and cls.discarded and cls.rule == "storage.routes[1]"
    assert discarded_page_types(cfg) == {"diary": "storage.routes[0]"}
    # A discard names no location, so it is not in the class -> location map.
    assert routed_storage_mapping(cfg) == {}


# --- AC8: only a rule discards ---------------------------------------------------


def test_nothing_is_discarded_without_a_rule() -> None:
    assert not route_for("person", "ssn", config={}).discarded
    assert not route_for("person", config={}).discarded
    assert route_for("person", "ssn", config=_cfg([])).rule is None
    assert discarded_page_types({}) == {}
    assert route_for_sensitivity_class("secret", {}) is None


# --- AC2: compile write ----------------------------------------------------------


def _raw(raw_dir: Path, name: str, content: str) -> RawFile:
    raw_dir.mkdir(parents=True, exist_ok=True)
    path = raw_dir / name
    path.write_text(content, encoding="utf-8")
    return RawFile(path=path, source=raw_dir.name, timestamp="", uuid8="")


def _compile_field_raw(tmp_path: Path, cfg: dict[str, Any]) -> tuple[Path, Any]:
    knowledge = tmp_path / "knowledge"
    wiki = knowledge / "wiki"
    wiki.mkdir(parents=True)
    raw = _raw(
        knowledge / "raw" / "sessions",
        "20260810T090000Z-aaaaaaaa.md",
        "---\nuid: note-1\ntype: reference\nname: Support ticket\naccess: internal\n"
        f"ticket_secret: {SENTINEL}\n---\n\nPlain body.\n",
    )
    client = MagicMock()
    client.messages.create.side_effect = AssertionError("tier 0 must not call the model")
    result = process_one(
        raw,
        EntityIndex(wiki),
        wiki,
        client,
        valid_types=["reference"],
        valid_tags=[],
        valid_access=["open", "internal", "confidential", "personal"],
        config=cfg,
    )
    return knowledge, result


def test_compile_writes_the_page_without_the_discarded_field(tmp_path: Path) -> None:
    cfg = _cfg([{"page_type": "reference", "fields": ["ticket_secret"], "to": []}])
    knowledge, result = _compile_field_raw(tmp_path, cfg)
    assert result.created
    (page,) = (knowledge / "wiki").glob("*.md")
    meta, body = parse_frontmatter(page.read_text(encoding="utf-8"))
    assert "ticket_secret" not in meta
    assert meta["name"] == "Support ticket" and meta["uid"] == "note-1"
    assert "Plain body." in body
    assert SENTINEL not in page.read_text(encoding="utf-8")
    assert discard_counts() == {"storage.routes[0]": 1}


def test_compile_keeps_the_field_when_no_rule_says_otherwise(tmp_path: Path) -> None:
    knowledge, _ = _compile_field_raw(tmp_path, _cfg([]))
    (page,) = (knowledge / "wiki").glob("*.md")
    assert SENTINEL in page.read_text(encoding="utf-8")
    assert discard_counts() == {}


def _batch_compile(tmp_path: Path, cfg: dict[str, Any], raw_text: str) -> Path:
    from athenaeum.batch import process_batch_run
    from athenaeum.models import TokenUsage
    from tests.test_batch_mode import FALLBACK_ACCESS, FALLBACK_TAGS, _FakeClient

    knowledge = tmp_path / "knowledge"
    wiki = knowledge / "wiki"
    wiki.mkdir(parents=True)
    _raw(knowledge / "raw" / "sessions", "20260810T090000Z-aaaaaaaa.md", raw_text)
    from athenaeum.intake import discover_raw_files

    process_batch_run(
        discover_raw_files(knowledge / "raw"),
        EntityIndex(wiki),
        wiki,
        _FakeClient(lambda params: "[]", allow_sync=False),
        ["reference"],
        FALLBACK_TAGS,
        FALLBACK_ACCESS,
        usage=TokenUsage(),
        config=cfg,
        max_api_calls=100,
    )
    return knowledge


_FIELD_RAW = (
    "---\nuid: note-1\ntype: reference\nname: Support ticket\naccess: internal\n"
    f"ticket_secret: {SENTINEL}\n---\n\nPlain body.\n"
)


def test_batch_compile_writes_the_page_without_the_discarded_field(tmp_path: Path) -> None:
    cfg = _cfg([{"page_type": "reference", "fields": ["ticket_secret"], "to": []}])
    knowledge = _batch_compile(tmp_path, cfg, _FIELD_RAW)
    (page,) = (knowledge / "wiki").glob("*.md")
    text = page.read_text(encoding="utf-8")
    meta, body = parse_frontmatter(text)
    assert "ticket_secret" not in meta
    assert meta["name"] == "Support ticket" and "Plain body." in body
    assert SENTINEL not in text
    assert discard_counts() == {"storage.routes[0]": 1}


def test_batch_compile_keeps_the_field_when_no_rule_says_otherwise(tmp_path: Path) -> None:
    knowledge = _batch_compile(tmp_path, _cfg([]), _FIELD_RAW)
    (page,) = (knowledge / "wiki").glob("*.md")
    assert SENTINEL in page.read_text(encoding="utf-8")


def test_batch_compile_discards_a_free_text_class(tmp_path: Path) -> None:
    cfg = _free_text_cfg([{"sensitivity_class": "pii", "to": []}])
    raw_text = (
        "---\nuid: note-2\ntype: reference\nname: Mail note\naccess: internal\n---\n\n"
        f"Reach them at {SENTINEL_EMAIL} soon.\n"
    )
    knowledge = _batch_compile(tmp_path, cfg, raw_text)
    pages = list((knowledge / "wiki").glob("*.md"))
    assert pages
    assert all(SENTINEL not in p.read_text(encoding="utf-8") for p in pages)
    assert "[sensitive:pii" in pages[0].read_text(encoding="utf-8")
    assert discard_counts() == {"storage.routes[0]": 1}


def test_screen_raw_for_compile_leaves_the_raw_file_on_disk_untouched(tmp_path: Path) -> None:
    from athenaeum.sensitivity_routing import screen_raw_for_compile

    raw = _raw(tmp_path / "raw", "a.md", _FIELD_RAW)
    cfg = _cfg([{"page_type": "reference", "fields": ["ticket_secret"], "to": []}])
    meta, _ = screen_raw_for_compile(raw, config=cfg, knowledge_root=tmp_path)
    assert "ticket_secret" not in meta
    assert SENTINEL in raw.path.read_text(encoding="utf-8")


# --- AC2: contact write ----------------------------------------------------------


def test_contact_write_discards_the_address_and_keeps_the_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = {
        **EXCLUDED_CONFIG,
        "storage": {
            "mapping": {"pii": "excluded"},
            "routes": [{"page_type": "person", "fields": ["emails"], "to": []}],
        },
    }
    knowledge, result = _compile(
        tmp_path,
        monkeypatch,
        note=_prose_note(address=SENTINEL_EMAIL),
        classified=[_person("Dana Quill")],
        config=cfg,
    )
    assert len(result.created) == 1  # type: ignore[attr-defined]
    page = knowledge / "wiki" / result.created[0].filename  # type: ignore[attr-defined]
    assert page.is_file()
    assert result.correspondent_contacts_recorded == 0  # type: ignore[attr-defined]
    contacts = pii.contacts_surface_root(knowledge, cfg)
    assert not contacts.exists() or list(contacts.rglob("*.md")) == []
    assert discard_counts() == {"storage.routes[0]": 1}


# --- AC2/AC3: field correction ----------------------------------------------------


def _wiki_root(tmp_path: Path) -> Path:
    wiki = tmp_path / "wiki"
    wiki.mkdir(parents=True)
    (wiki / "alex.md").write_text(
        f"---\nuid: {UID}\ntype: person\nname: Alex Doe\n---\nBody.\n", encoding="utf-8"
    )
    return tmp_path


def _corr_cfg(routes: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "librarian": {
            "corrections": {
                "fields": {"ssn": {"shape": "scalar", "writers": ["intake-bot"]}}
            }
        },
        "storage": {"routes": routes},
    }


DISCARD_SSN = [{"page_type": "person", "fields": ["ssn"], "to": []}]
ENVELOPE = {
    "record": "batch",
    "schema_version": 1,
    "submitter": "intake-bot",
    "batch_id": "20260806T140211Z-9f3ac1d2",
    "created_at": "2026-08-06T14:02:11Z",
    "defaults": {},
}


def _record(field: str = "ssn", op: str = "set", target: Any = None) -> dict[str, Any]:
    return {
        "record": "correction",
        "target": target or {"uid": UID},
        "op": op,
        "field": field,
        "value": SENTINEL,
        "source": "api:intake-bot:2026-08-06",
        "observed_at": "2026-08-06T14:01:55Z",
    }


def _apply(root: Path, cfg: dict[str, Any], record: dict[str, Any]) -> Any:
    return process_correction_record(
        record,
        ENVELOPE,
        index=EntityIndex(root / "wiki"),
        knowledge_root=root,
        registry_entities={UID: {"type": "person", "handles": {}}},
        config=cfg,
    )


def _tree(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()
    }


def test_correction_to_a_discarded_field_ends_discarded_and_writes_nothing(
    tmp_path: Path,
) -> None:
    root = _wiki_root(tmp_path)
    before = _tree(root)
    result = _apply(root, _corr_cfg(DISCARD_SSN), _record())
    assert result.disposition == "discarded"
    assert result.value is None and result.target is None
    assert result.correction_id == ""
    assert SENTINEL not in repr(result)
    assert _tree(root) == before
    assert discard_counts() == {"storage.routes[0]": 1}


def test_correction_to_an_undiscarded_field_is_unchanged(tmp_path: Path) -> None:
    root = _wiki_root(tmp_path)
    result = _apply(root, _corr_cfg([]), _record())
    assert result.disposition == "applied"
    assert discard_counts() == {}


def test_discarded_correction_never_escalates_and_leaves_no_trace(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    root = _wiki_root(tmp_path)
    src = root / "raw" / "intake-bot"
    src.mkdir(parents=True)
    batch = src / "20260806T140211Z-9f3ac1d2.jsonl"
    batch.write_text(json.dumps(ENVELOPE) + "\n" + json.dumps(_record()) + "\n")
    escalated: list[Any] = []
    caplog.set_level(logging.DEBUG)
    summary = run_correction_phase(
        raw_root=root / "raw",
        wiki_root=root / "wiki",
        knowledge_root=root,
        index=EntityIndex(root / "wiki"),
        config=_corr_cfg(DISCARD_SSN),
        escalate_one=lambda r, o: escalated.append(r) or True,
    )
    assert summary["dispositions"] == {"discarded": 1}
    assert escalated == []
    ledger = (root / "wiki" / "_corrections_applied.jsonl").read_text(encoding="utf-8")
    assert json.loads(ledger)["dispositions"] == {"discarded": 1}
    assert json.loads(ledger)["non_trivial_correction_ids"] == []
    assert json.loads(ledger)["redacted_correction_payloads"] == []
    # The id the record would have had, computed from its value, is nowhere.
    from athenaeum.corrections import compute_correction_id

    value_id = compute_correction_id(
        schema_version=1, target={"uid": UID}, op="set", field_name="ssn", value=SENTINEL
    )
    assert SENTINEL not in ledger and value_id not in ledger
    assert SENTINEL not in caplog.text and value_id not in caplog.text
    handoffs = [p for p in root.rglob("*") if p.is_file() and p != batch]
    for path in handoffs:
        text = path.read_text(encoding="utf-8", errors="replace")
        assert SENTINEL not in text and value_id not in text


def test_a_failure_before_the_page_type_is_known_does_not_leak_the_value(
    tmp_path: Path,
) -> None:
    # The target is unresolvable, so no page type is known; the field is named
    # by a discard rule on some page type, so the record is discarded rather
    # than raised with its value into a handoff note.
    root = _wiki_root(tmp_path)
    result = _apply(root, _corr_cfg(DISCARD_SSN), _record(target={"uid": "nobody"}))
    assert result.disposition == "discarded"
    assert result.value is None


def test_ledger_denominator_counts_a_discarded_record(tmp_path: Path) -> None:
    root = _wiki_root(tmp_path)
    src = root / "raw" / "intake-bot"
    src.mkdir(parents=True)
    batch = src / "b.jsonl"
    batch.write_text(json.dumps(ENVELOPE) + "\n" + json.dumps(_record()) + "\n")
    outcome = process_batch_file(
        batch,
        ENVELOPE,
        "intake-bot",
        index=EntityIndex(root / "wiki"),
        knowledge_root=root,
        config=_corr_cfg(DISCARD_SSN),
        registry_entities={UID: {"type": "person", "handles": {}}},
    )
    assert build_ledger_record(outcome)["dispositions"] == {"discarded": 1}
    assert outcome.all_terminal()


# --- AC4: free text -----------------------------------------------------------------


def _free_text_cfg(routes: list[dict[str, Any]]) -> dict[str, Any]:
    return {"sensitivity": {"routing": {"enabled": True}}, "storage": {"routes": routes}}


def test_discarded_class_in_free_text_becomes_a_fixed_marker(tmp_path: Path) -> None:
    cfg = _free_text_cfg([{"sensitivity_class": "pii", "to": []}])
    out = route_sensitive_values(
        raw_ref="raw/a.md",
        text=f"Mail {SENTINEL_EMAIL} now. Or {SENTINEL_EMAIL} again.",
        frontmatter={},
        config=cfg,
        knowledge_root=tmp_path,
    )
    assert SENTINEL not in out
    marker = "[sensitive:pii — value not stored (discarded)]"
    assert out == f"Mail {marker} now. Or {marker} again."
    # No record, no vault directory, nothing on disk.
    assert list(tmp_path.rglob("*")) == []
    assert discard_counts() == {"storage.routes[0]": 2}
    # Nothing to resolve a discarded value from.
    assert resolve_sensitive_record("pii", "0" * 32, cfg, tmp_path) is None


# --- AC9: no rule is refused at load --------------------------------------------------


def test_routing_on_with_no_rule_is_refused_naming_the_rule_to_write(
    tmp_path: Path,
) -> None:
    cfg = {"sensitivity": {"routing": {"enabled": True}}}
    with pytest.raises(SensitivityRoutingError) as exc:
        refuse_unrouted_value_classes(cfg)
    message = str(exc.value)
    assert "storage.routes" in message and "sensitivity_class: pii" in message
    assert "to: []" in message
    # Not stored in the built-in excluded location, and not discarded.
    with pytest.raises(SensitivityRoutingError):
        route_sensitive_values(
            raw_ref="raw/a.md",
            text=f"Mail {SENTINEL_EMAIL}",
            frontmatter={},
            config=cfg,
            knowledge_root=tmp_path,
        )
    assert list(tmp_path.rglob("*")) == []
    assert discard_counts() == {}


def test_routing_off_or_rule_present_or_class_switched_off_is_not_refused() -> None:
    refuse_unrouted_value_classes({})
    refuse_unrouted_value_classes(_free_text_cfg([{"sensitivity_class": "pii", "to": []}]))
    refuse_unrouted_value_classes(
        _free_text_cfg([{"sensitivity_class": "pii", "to": ["excluded"]}])
    )
    off = {
        "sensitivity": {"routing": {"enabled": True, "classes": {"pii": {"action": "off"}}}}
    }
    refuse_unrouted_value_classes(off)


def test_run_refuses_at_load(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from athenaeum import librarian

    knowledge = tmp_path / "knowledge"
    (knowledge / "wiki").mkdir(parents=True)
    (knowledge / "raw").mkdir()
    monkeypatch.setattr(
        librarian, "load_config", lambda _root: {"sensitivity": {"routing": {"enabled": True}}}
    )
    code = librarian.run(
        raw_root=knowledge / "raw", wiki_root=knowledge / "wiki", knowledge_root=knowledge
    )
    assert code == 1


# --- AC5: a whole page type is not captured ----------------------------------------


def test_decision_for_a_discarded_page_type_is_not_captured_naming_the_rule() -> None:
    cfg = _cfg([{"page_type": "diary", "to": []}])
    discarded = discarded_page_types(cfg)
    dropped = decide(Description(type="diary"), RelevancePolicy(), discarded=discarded)
    assert dropped.capture == CAPTURE_DROP
    assert dropped.reason == REASON_DISCARD
    assert dropped.discard_rule == "storage.routes[0]"
    # An explicit "remember this" does not override a discard.
    explicit = decide(
        Description(type="diary", explicit=True), RelevancePolicy(), discarded=discarded
    )
    assert explicit.capture == CAPTURE_DROP
    assert decide(Description(type="note"), RelevancePolicy(), discarded=discarded).capture == (
        CAPTURE_KEEP
    )


def test_capture_gate_drops_the_discarded_type_with_no_excerpt() -> None:
    discarded = {"diary": "storage.routes[0]"}
    content = f"---\ntype: diary\nname: Day one\n---\n\n{SENTINEL}\n"
    gate = gate_raw_content(None, content, discarded=discarded)
    assert gate.fully_dropped
    assert "storage.routes[0]" in gate.reason
    assert all(SENTINEL not in d.excerpt for d in gate.drops)
    kept = gate_raw_content(None, "---\ntype: note\nname: x\n---\n\nbody\n", discarded=discarded)
    assert not kept.drops


def test_process_one_does_not_capture_a_discarded_page_type(tmp_path: Path) -> None:
    knowledge = tmp_path / "knowledge"
    wiki = knowledge / "wiki"
    wiki.mkdir(parents=True)
    raw = _raw(
        knowledge / "raw" / "sessions",
        "20260810T090000Z-bbbbbbbb.md",
        f"---\nuid: d-1\ntype: reference\nname: Day one\n---\n\n{SENTINEL}\n",
    )
    client = MagicMock()
    client.messages.create.side_effect = AssertionError("not captured: no model call")
    result = process_one(
        raw,
        EntityIndex(wiki),
        wiki,
        client,
        valid_types=["reference"],
        valid_tags=[],
        valid_access=["open", "internal", "confidential", "personal"],
        config=_cfg([{"page_type": "reference", "to": []}]),
    )
    assert not result.created
    assert any("storage.routes[0]" in s for s in result.skipped)
    assert list(wiki.glob("*.md")) == []


# --- AC7: one counter per rule in the run summary -------------------------------------


def test_run_summary_carries_one_counter_per_rule_and_no_value(tmp_path: Path) -> None:
    cfg = _cfg(
        [
            {"page_type": "person", "fields": ["ssn"], "to": []},
            {"sensitivity_class": "pii", "to": []},
        ],
        sensitivity={"routing": {"enabled": True}},
    )
    route_sensitive_values(
        raw_ref="raw/a.md",
        text=f"{SENTINEL_EMAIL} and {SENTINEL_EMAIL}",
        frontmatter={},
        config=cfg,
        knowledge_root=tmp_path,
    )
    root = _wiki_root(tmp_path / "w")
    _apply(root, _corr_cfg(DISCARD_SSN), _record())
    ctx = _make_ctx(tmp_path)
    ctx.emit_run_summary()
    (entry,) = [e for e in ctx.run_profile if e[0] == "discard"]
    assert entry[2] == {"reason": "completed", "storage.routes[0]": 1, "storage.routes[1]": 2}
    record = build_run_summary_ledger_record(ctx.run_profile)
    assert record["phases"]["discard"]["storage.routes[1]"] == 2
    line = _render_run_summary(ctx.run_profile)
    assert "storage.routes[1]=2" in line
    assert SENTINEL not in line and SENTINEL not in json.dumps(record)


def test_run_summary_omits_the_discard_phase_when_nothing_was_discarded(tmp_path: Path) -> None:
    ctx = _make_ctx(tmp_path)
    ctx.emit_run_summary()
    assert [e for e in ctx.run_profile if e[0] == "discard"] == []


# --- AC6: the sentinel ---------------------------------------------------------------


def _leaks(dirs: list[Path], allowed: set[Path]) -> list[Path]:
    found: list[Path] = []
    for base in dirs:
        if not base.exists():
            continue
        for path in base.rglob("*"):
            if SENTINEL in str(path.relative_to(base)):
                found.append(path)
            elif path.is_file() and path not in allowed:
                if SENTINEL.encode() in path.read_bytes():
                    found.append(path)
    return found


def test_sentinel_appears_in_no_file_other_than_the_raw_the_test_wrote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = tmp_path / "cache"
    cache.mkdir(exist_ok=True)
    allowed: set[Path] = set()

    # 1. compile write
    k1 = tmp_path / "k1"
    k1.mkdir()
    cfg1 = _cfg([{"page_type": "reference", "fields": ["ticket_secret"], "to": []}])
    knowledge, _ = _compile_field_raw(k1, cfg1)
    allowed.update((knowledge / "raw").rglob("*.md"))

    # 2. contact write
    k2 = tmp_path / "k2"
    k2.mkdir()
    cfg2 = {
        "storage": {
            "mapping": {"pii": "excluded"},
            "routes": [{"page_type": "person", "fields": ["emails"], "to": []}],
        }
    }
    knowledge2, _ = _compile(
        k2,
        monkeypatch,
        note=_prose_note(address=SENTINEL_EMAIL),
        classified=[_person("Dana Quill")],
        config=cfg2,
    )
    allowed.update((knowledge2 / "raw").rglob("*.md"))

    # 3. field correction, through the whole phase
    root = _wiki_root(tmp_path / "k3")
    src = root / "raw" / "intake-bot"
    src.mkdir(parents=True)
    batch = src / "b.jsonl"
    batch.write_text(json.dumps(ENVELOPE) + "\n" + json.dumps(_record()) + "\n")
    allowed.add(batch)
    run_correction_phase(
        raw_root=root / "raw",
        wiki_root=root / "wiki",
        knowledge_root=root,
        index=EntityIndex(root / "wiki"),
        config=_corr_cfg(DISCARD_SSN),
        escalate_one=lambda r, o: True,
    )

    # 4. free text
    k4 = tmp_path / "k4"
    k4.mkdir()
    route_sensitive_values(
        raw_ref="raw/a.md",
        text=f"Mail {SENTINEL_EMAIL}",
        frontmatter={},
        config=_free_text_cfg([{"sensitivity_class": "pii", "to": []}]),
        knowledge_root=k4,
    )

    # 5. batch compile write
    k6 = tmp_path / "k6"
    k6.mkdir()
    knowledge6 = _batch_compile(k6, cfg1, _FIELD_RAW)
    allowed.update((knowledge6 / "raw").rglob("*.md"))

    assert _leaks([k1, k2, root, k4, k6, cache], allowed) == []
    # Positive control: the scan does see the sentinel where it is kept.
    kept = tmp_path / "k5"
    kept.mkdir()
    (kept / "page.md").write_text(SENTINEL, encoding="utf-8")
    assert _leaks([kept], set()) == [kept / "page.md"]


# --- A record stored before routing demanded a rule is still readable ----------------


def test_legacy_record_in_the_excluded_location_resolves_with_no_rule(tmp_path: Path) -> None:
    cfg = {"sensitivity": {"routing": {"enabled": True}}}
    record_id = "1" * 32
    vault = tmp_path / "excluded" / "sensitivity" / "pii"
    vault.mkdir(parents=True)
    (vault / f"{record_id}.md").write_text(
        _render_vault_record(
            record_id=record_id,
            sensitivity_class="pii",
            source_ref="raw/a.md",
            value=SENTINEL_EMAIL,
        ),
        encoding="utf-8",
    )
    resolved = resolve_sensitive_record("pii", record_id, cfg, tmp_path)
    assert resolved == SENTINEL_EMAIL
    # Writes stay refused.
    with pytest.raises(SensitivityRoutingError):
        route_sensitive_values(
            raw_ref="raw/a.md",
            text=f"Mail {SENTINEL_EMAIL}",
            frontmatter={},
            config=cfg,
            knowledge_root=tmp_path,
        )
