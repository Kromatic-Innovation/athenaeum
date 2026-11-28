# SPDX-License-Identifier: Apache-2.0
"""A withheld correction value goes to a vault, or nowhere (athenaeum#1419)."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

import pytest

from athenaeum import corrections, pii
from athenaeum.corrections import BatchOutcome, load_registry
from athenaeum.models import EntityIndex, parse_frontmatter

UID = "person-a"
OTHER_UID = "person-b"
MARKER = "zz-withheld-marker-zz"
OFF = {"embedded": False, "recallable": False, "merge_eligible": False}
SUBMITTER = "enrichment-service"
BATCH_ID = "20260806T030000Z-1a2b3c4d"


def _config(rules: list[dict[str, Any]] | None) -> dict[str, Any]:
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
    if rules is not None:
        cfg["storage"]["routes"] = rules
    return cfg


ROUTE_EMAILS = [{"page_type": "person", "fields": ["emails"], "to": ["vault"]}]
ROUTE_WHOLE = [{"page_type": "person", "to": ["vault"]}]
ROUTE_EMPTY = [{"page_type": "person", "fields": ["emails"], "to": []}]
ROUTE_NOTHING_FOR_PERSON = [{"page_type": "organization", "fields": ["x"], "to": ["vault"]}]


def _seed(tmp_path: Path, *uids: str) -> Path:
    wiki = tmp_path / "wiki"
    wiki.mkdir(parents=True, exist_ok=True)
    for uid in uids:
        (wiki / f"{uid}.md").write_text(
            f"---\nuid: {uid}\ntype: person\nname: {uid}\n---\nBody.\n", encoding="utf-8"
        )
    return tmp_path


def _outcome(
    tmp_path: Path,
    config: dict[str, Any],
    target: dict[str, Any],
    *,
    field: str = "favourite_colour",
    value: str = MARKER,
) -> BatchOutcome:
    """A real outcome: the record goes through the tier-0 applier, dry-run."""
    envelope = {
        "record": "batch",
        "schema_version": 1,
        "submitter": SUBMITTER,
        "batch_id": BATCH_ID,
    }
    raw = {
        "record": "correction",
        "target": target,
        "op": "set",
        "field": field,
        "value": value,
        "source": "api:enrichment-vendor",
        "observed_at": "2026-08-06T05:58:40Z",
    }
    result = corrections.process_correction_record(
        raw,
        envelope,
        index=EntityIndex(tmp_path / "wiki"),
        knowledge_root=tmp_path,
        registry_entities=load_registry(tmp_path),
        config=config,
        dry_run=True,
    )
    assert result.disposition == "raised-tier"
    assert result.reason == corrections.ALLOWLIST_REJECTION_REASON
    result.ordinal = 0
    return BatchOutcome(
        path=tmp_path / "raw" / SUBMITTER / f"{BATCH_ID}.jsonl",
        source=SUBMITTER,
        envelope=envelope,
        records_total=1,
        results=[result],
    )


def _store(tmp_path: Path, config: dict[str, Any], outcome: BatchOutcome) -> dict[str, str]:
    return corrections.store_withheld_corrections(
        outcome,
        knowledge_root=tmp_path,
        config=config,
        index=EntityIndex(tmp_path / "wiki"),
        registry_entities=load_registry(tmp_path),
    )


def _everywhere_but_batch(tmp_path: Path) -> list[Path]:
    """Files holding the marker under the knowledge root and the cache dir."""
    hits: list[Path] = []
    roots = [tmp_path, Path(os.environ["ATHENAEUM_CACHE_DIR"])]
    for root in roots:
        if not root.exists():
            continue
        for p in root.rglob("*"):
            if p.is_file() and MARKER.encode() in p.read_bytes():
                hits.append(p)
    return hits


def _write_batch_file(tmp_path: Path) -> Path:
    raw = tmp_path / "raw" / SUBMITTER
    raw.mkdir(parents=True, exist_ok=True)
    path = raw / f"{BATCH_ID}.jsonl"
    path.write_text(f'{{"value": "{MARKER}"}}\n', encoding="utf-8")
    return path


def _vault_sections(tmp_path: Path, uid: str = UID) -> dict[str, Any]:
    meta, _ = parse_frontmatter((tmp_path / "vault" / f"{uid}.md").read_text(encoding="utf-8"))
    return (meta.get("sections") or {}).get("withheld_corrections", {})


def test_section_is_registered() -> None:
    assert "withheld_corrections" in pii.RECORD_SECTIONS


@pytest.mark.parametrize("rules", [ROUTE_EMAILS, ROUTE_WHOLE])
def test_value_is_stored_in_the_vault_with_its_context(
    tmp_path: Path, rules: list[dict[str, Any]]
) -> None:
    """A page-type primary vault or a whole-type route is the home; context is kept."""
    _seed(tmp_path, UID)
    config = _config(rules)
    outcome = _outcome(tmp_path, config, {"uid": UID})
    placed = _store(tmp_path, config, outcome)
    cid = outcome.results[0].correction_id
    assert placed == {cid: "vault"}
    entry = _vault_sections(tmp_path)[cid]
    assert entry == {
        "field": "favourite_colour",
        "op": "set",
        "value": MARKER,
        "reason": corrections.ALLOWLIST_REJECTION_REASON,
        "batch_id": BATCH_ID,
        "ref": f"{BATCH_ID}#0",
    }


def test_the_field_route_is_the_home_when_a_rule_routes_the_field(tmp_path: Path) -> None:
    """The field's own vault comes first (no record is created elsewhere)."""
    _seed(tmp_path, UID)
    rules = [
        {"page_type": "person", "fields": ["favourite_colour"], "to": ["vault"]},
    ]
    config = _config(rules)
    outcome = _outcome(tmp_path, config, {"uid": UID})
    assert pii.record_placement(
        tmp_path, config, UID, field="favourite_colour", sensitive=True
    ).targets == ("vault",)
    assert _store(tmp_path, config, outcome) == {outcome.results[0].correction_id: "vault"}


def test_round_trip_through_the_entity_read(tmp_path: Path) -> None:
    """The value with the sensitive-read flag, a marker without it."""
    _seed(tmp_path, UID)
    config = _config(ROUTE_EMAILS)
    outcome = _outcome(tmp_path, config, {"uid": UID})
    _store(tmp_path, config, outcome)
    cid = outcome.results[0].correction_id

    shown = pii.entity_read_by_uid(tmp_path, config, UID, include_excluded=True)
    assert shown is not None
    assert shown.sections["withheld_corrections"][cid]["value"] == MARKER

    hidden = pii.entity_read_by_uid(tmp_path, config, UID, include_excluded=False)
    assert hidden is not None
    assert "withheld_corrections" not in hidden.sections
    assert MARKER not in repr(hidden)
    marked = {m.field: m.value_count for m in hidden.section_redactions}
    assert marked["sections.withheld_corrections"] == 1


def test_no_entity_is_created_and_no_page_changes(tmp_path: Path) -> None:
    """Storing a withheld value creates no entity and changes no page."""
    _seed(tmp_path, UID)
    config = _config(ROUTE_EMAILS)
    page = tmp_path / "wiki" / f"{UID}.md"
    before = page.read_bytes()
    pages_before = sorted(p.name for p in (tmp_path / "wiki").iterdir())
    outcome = _outcome(tmp_path, config, {"uid": UID})
    _store(tmp_path, config, outcome)
    assert page.read_bytes() == before
    assert sorted(p.name for p in (tmp_path / "wiki").iterdir()) == pages_before
    assert (tmp_path / "vault" / f"{UID}.md").exists()  # a rule names the location
    assert sorted(p.name for p in (tmp_path / "vault").iterdir()) == [f"{UID}.md"]


@pytest.mark.parametrize(
    ("rules", "target", "why"),
    [
        (None, {"uid": UID}, "no rule at all"),
        (ROUTE_NOTHING_FOR_PERSON, {"uid": UID}, "no rule for the page type"),
        (ROUTE_EMPTY, {"uid": UID}, "an empty-target rule"),
        (ROUTE_EMAILS, {"uid": "person-missing"}, "target does not resolve"),
        (ROUTE_EMAILS, {"type": "person", "name": "Nobody Here"}, "name does not resolve"),
    ],
)
def test_nowhere_leaves_the_marker_only_in_the_batch_file(
    tmp_path: Path, rules: list[dict[str, Any]] | None, target: dict[str, Any], why: str
) -> None:
    """With no home for the value, it is recorded nowhere but the submitter's batch file."""
    _seed(tmp_path, UID)
    batch = _write_batch_file(tmp_path)
    config = _config(rules)
    outcome = _outcome(tmp_path, config, target)
    assert _store(tmp_path, config, outcome) == {outcome.results[0].correction_id: "nowhere"}, why
    assert _everywhere_but_batch(tmp_path) == [batch], why


def test_an_ambiguous_target_is_unplaceable_and_stored_nowhere(tmp_path: Path) -> None:
    """A handle held by two entities."""
    _seed(tmp_path, UID, OTHER_UID)
    handle = {"alt_emails": ["x@example.org"]}
    (tmp_path / "registry.json").write_text(
        json.dumps(
            {
                "entities": {
                    UID: {"type": "person", "handles": handle},
                    OTHER_UID: {"type": "person", "handles": handle},
                }
            }
        ),
        encoding="utf-8",
    )
    batch = _write_batch_file(tmp_path)
    config = _config(ROUTE_EMAILS)
    target = {"type": "person", "handle": {"alt_emails": "x@example.org"}}
    outcome = _outcome(tmp_path, config, target)
    assert _store(tmp_path, config, outcome) == {outcome.results[0].correction_id: "nowhere"}
    assert _everywhere_but_batch(tmp_path) == [batch]


def test_the_disposition_and_reason_are_unchanged(tmp_path: Path) -> None:
    """The raised disposition and reason survive the placement."""
    _seed(tmp_path, UID)
    config = _config(ROUTE_EMAILS)
    outcome = _outcome(tmp_path, config, {"uid": "person-missing"})
    _store(tmp_path, config, outcome)
    assert outcome.results[0].disposition == "raised-tier"
    assert outcome.results[0].reason == corrections.ALLOWLIST_REJECTION_REASON


def test_a_non_routed_field_is_vaulted_or_nowhere_never_in_a_tracked_file(
    tmp_path: Path,
) -> None:
    """A non-routed, non-allowlisted field, with a rule for another field and with none."""
    _seed(tmp_path, UID)
    batch = _write_batch_file(tmp_path)

    routed = _config(ROUTE_EMAILS)
    outcome = _outcome(tmp_path, routed, {"uid": UID}, field="job_hobby")
    assert _store(tmp_path, routed, outcome) == {outcome.results[0].correction_id: "vault"}
    hits = _everywhere_but_batch(tmp_path)
    assert sorted(hits) == sorted([batch, tmp_path / "vault" / f"{UID}.md"])

    (tmp_path / "vault" / f"{UID}.md").unlink()
    bare = _config(None)
    outcome = _outcome(tmp_path, bare, {"uid": UID}, field="job_hobby")
    assert _store(tmp_path, bare, outcome) == {outcome.results[0].correction_id: "nowhere"}
    assert _everywhere_but_batch(tmp_path) == [batch]


def test_writing_twice_is_idempotent(tmp_path: Path) -> None:
    """Storing the same outcome twice leaves the vault record unchanged."""
    _seed(tmp_path, UID)
    config = _config(ROUTE_EMAILS)
    outcome = _outcome(tmp_path, config, {"uid": UID})
    _store(tmp_path, config, outcome)
    first = (tmp_path / "vault" / f"{UID}.md").read_bytes()
    assert _store(tmp_path, config, outcome) == {outcome.results[0].correction_id: "vault"}
    assert (tmp_path / "vault" / f"{UID}.md").read_bytes() == first


def test_only_allowlist_rejections_are_collected(tmp_path: Path) -> None:
    _seed(tmp_path, UID)
    config = _config(ROUTE_EMAILS)
    outcome = _outcome(tmp_path, config, {"uid": UID})
    outcome.results[0].reason = "some other reason"
    assert _store(tmp_path, config, outcome) == {}
    assert not (tmp_path / "vault").exists()


def test_no_log_line_carries_the_value(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """No log line carries the value, on both the vault and the nowhere paths."""
    _seed(tmp_path, UID)
    with caplog.at_level(logging.DEBUG):
        for rules in (ROUTE_EMAILS, None):
            config = _config(rules)
            outcome = _outcome(tmp_path, config, {"uid": UID})
            _store(tmp_path, config, outcome)
            _store(tmp_path, config, outcome)
    assert caplog.records
    assert MARKER not in caplog.text
    assert all(MARKER not in r.getMessage() for r in caplog.records)


def test_the_correction_phase_stores_the_value_in_the_vault(tmp_path: Path) -> None:
    """The phase calls the function where the ledger line is built."""
    from athenaeum.librarian import _run_correction_phase
    from tests.test_librarian_corrections import _batch, _make_ctx

    _seed(tmp_path, UID)
    config = _config(ROUTE_EMAILS)
    raw = tmp_path / "raw" / SUBMITTER
    raw.mkdir(parents=True)
    (raw / f"{BATCH_ID}.jsonl").write_text(
        _batch(
            {
                "record": "correction",
                "target": {"uid": UID},
                "op": "set",
                "field": "favourite_colour",
                "value": MARKER,
                "source": "api:enrichment-vendor",
                "observed_at": "2026-08-06T05:58:40Z",
            },
            submitter=SUBMITTER,
        ),
        encoding="utf-8",
    )
    _run_correction_phase(_make_ctx(tmp_path, config))
    entries = _vault_sections(tmp_path)
    assert [e["value"] for e in entries.values()] == [MARKER]
    # Neither the ledger line nor the handoff note carries the value; both
    # name the vault reference.
    ledger = (tmp_path / "wiki" / "_corrections_applied.jsonl").read_text(encoding="utf-8")
    assert MARKER not in ledger
    assert json.loads(ledger)["withheld"] == [{"ref": 0, "stored": "vault"}]
    (note,) = sorted(raw.glob("*.md"))
    text = note.read_text(encoding="utf-8")
    assert MARKER not in text
    assert f"value withheld; stored in the vault under reference {BATCH_ID}#0" in text
