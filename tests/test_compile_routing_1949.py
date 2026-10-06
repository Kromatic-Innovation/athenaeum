# SPDX-License-Identifier: Apache-2.0
"""Prose-compile routing: target the correspondent's page, and record where writes went.

Issue athenaeum#1949. A scripted submitter writes short prose files: one
correspondent and a one-paragraph conversation summary, with the correspondent
identified in frontmatter. One of the two observed cases is a silent drop PLUS
an unrelated write:

    The compile run that processed the file reported one update, but it was
    applied to a DIFFERENT person page -- a body rewrite with no citation of
    the file and none of the summary's content. Afterwards, no page in the
    wiki cited the file. The run's log had since rotated, so the entity
    routing for that run could not be reconstructed.

Cases:

- ``TestClassifyRoute`` — the verdict logic in isolation, including why a
  divergent write is PREVENTED in one situation and merely LOGGED in another.
- ``TestCorrectPage`` — AC1's satisfied case: the summary lands on the
  correspondent's page and nothing is refused.
- ``TestWrongPageMisroute`` — AC1's defect case: the write to another person's
  page is refused, that page is left byte-for-byte untouched, and the case
  escalates.
- ``TestMultiPersonCompilationStillWorks`` — the regression guard on the
  above: when the correspondent's page IS written, a second person's page is
  logged rather than refused.
- ``TestRouteLedger`` — AC2: each compile records which uid(s) it wrote for
  each raw file, durably, so a misroute is reconstructible after the log
  rotates.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from athenaeum import pii
from athenaeum.compile_routing import (
    CORRESPONDENT_EMAIL_KEY,
    DIVERGENCE_LOGGED,
    DIVERGENCE_PREVENTED,
    CompileRouteRecord,
    classify_route,
    compile_route_ledger_path,
    correspondent_from_raw,
    page_uid,
    read_compile_routes,
    record_compile_route,
    resolved_correspondent_uid,
)
from athenaeum.librarian import process_one
from athenaeum.models import ClassifiedEntity, EntityIndex, RawFile, TokenUsage

EXCLUDED_CONFIG: dict[str, object] = {"storage": {"mapping": {"pii": "excluded"}}}
VALID_TYPES = ["person", "company", "concept", "reference"]
VALID_ACCESS = ["public", "internal", "confidential"]

_ADDRESS = "dana.quill@example.org"
_DISPLAY = "Dana Quill"
_DANA_UID = "uid-dana"
_ROBIN_UID = "uid-robin"
_OBSERVED_AT = "2026-09-30T11:04:00Z"


def _prose_note(*, address: str = _ADDRESS, with_key: bool = True) -> str:
    """A synthetic, PII-free one-correspondent conversation summary."""
    lines = [
        "---",
        "schema_version: 1",
        'source: "script:voltaire"',
        'submitter: "voltaire"',
        f'observed_at: "{_OBSERVED_AT}"',
        'thread_id: "thread-synthetic-0001"',
    ]
    if with_key:
        lines.append(f'{CORRESPONDENT_EMAIL_KEY}: "{address}"')
    # NOTE: deliberately no `correspondent_name`. That key is athenaeum#1948's
    # tiebreaker for which of several CREATED pages is the correspondent; here
    # it would also be a tier-1 programmatic index-key hit on the page name,
    # adding a second update action for the same page and obscuring which
    # routing decision the assertion is about. The guard keys on the ADDRESS,
    # which is exactly what these cases need to exercise.
    lines += [
        "---",
        "",
        "# Pricing follow-up",
        "",
        "They asked about tiered pricing before the renewal.",
        "",
    ]
    return "\n".join(lines)


def _raw(raw_dir: Path, content: str, filename: str = "summary.md") -> RawFile:
    raw_dir.mkdir(parents=True, exist_ok=True)
    path = raw_dir / filename
    path.write_text(content, encoding="utf-8")
    return RawFile(path=path, source=raw_dir.name, timestamp="", uuid8="")


def _write_page(
    wiki_root: Path, uid: str, *, name: str, entity_type: str = "person"
) -> Path:
    wiki_root.mkdir(parents=True, exist_ok=True)
    path = wiki_root / f"{uid}.md"
    path.write_text(
        f"---\nuid: {uid}\nname: {name}\ntype: {entity_type}\n"
        f"created: 2026-01-01\nupdated: 2026-01-01\n---\n\nNotes about {name}.\n",
        encoding="utf-8",
    )
    return path


def _write_record(knowledge: Path, filename: str, *, uid: str, emails: list[str]) -> Path:
    root = pii.contacts_surface_root(knowledge, EXCLUDED_CONFIG)
    root.mkdir(parents=True, exist_ok=True)
    listed = "".join(f"  - {address}\n" for address in emails)
    path = root / filename
    path.write_text(
        f"---\nuid: {uid}\npii: true\nemails:\n{listed}---\n\nContact record.\n",
        encoding="utf-8",
    )
    return path


def _pending(uid_type: str, name: str = "p") -> tuple[Path, str]:
    """One ``pending_updates`` entry whose rendered frontmatter declares a type."""
    return (
        Path(f"{name}.md"),
        f"---\nuid: {name}\ntype: {uid_type}\n---\n\nbody\n",
    )


# ---------------------------------------------------------------------------
# The verdict logic, in isolation
# ---------------------------------------------------------------------------


class TestClassifyRoute:
    def test_no_correspondent_is_inactive_and_changes_nothing(self) -> None:
        verdict = classify_route(
            correspondent_uid="",
            pending_updates=[_pending("person", "other")],
            updated_uids=["uid-other"],
        )
        assert not verdict.active
        assert verdict.prevented == ()
        assert verdict.logged == ()

    def test_update_to_the_correspondent_is_never_divergent(self) -> None:
        verdict = classify_route(
            correspondent_uid=_DANA_UID,
            pending_updates=[_pending("person", "dana")],
            updated_uids=[_DANA_UID],
        )
        assert verdict.active
        assert verdict.divergent == ()

    def test_other_person_page_with_no_write_to_the_correspondent_is_prevented(
        self,
    ) -> None:
        """The observed misroute: a silent drop plus an unrelated rewrite."""
        verdict = classify_route(
            correspondent_uid=_DANA_UID,
            pending_updates=[_pending("person", "robin")],
            updated_uids=[_ROBIN_UID],
        )
        assert verdict.prevented == (0,)
        assert verdict.logged == ()
        assert any(DIVERGENCE_PREVENTED in reason for reason in verdict.reasons)
        assert any(_DANA_UID in reason for reason in verdict.reasons)

    def test_other_person_page_is_only_logged_when_the_correspondent_was_written(
        self,
    ) -> None:
        """Ordinary multi-person compilation must not be refused."""
        verdict = classify_route(
            correspondent_uid=_DANA_UID,
            pending_updates=[_pending("person", "dana"), _pending("person", "robin")],
            updated_uids=[_DANA_UID, _ROBIN_UID],
        )
        assert verdict.prevented == ()
        assert verdict.logged == (1,)
        assert any(DIVERGENCE_LOGGED in reason for reason in verdict.reasons)

    @pytest.mark.parametrize("other_type", ["company", "concept", "reference"])
    def test_a_non_person_update_is_never_divergent(self, other_type: str) -> None:
        verdict = classify_route(
            correspondent_uid=_DANA_UID,
            pending_updates=[_pending(other_type, "acme")],
            updated_uids=["uid-acme"],
        )
        assert verdict.divergent == ()

    def test_a_create_of_the_correspondent_counts_as_reaching_them(self) -> None:
        verdict = classify_route(
            correspondent_uid=_DANA_UID,
            pending_updates=[_pending("person", "robin")],
            updated_uids=[_ROBIN_UID],
            created_uids=[_DANA_UID],
        )
        assert verdict.prevented == ()
        assert verdict.logged == (0,)


class TestCorrespondentReader:
    def test_reads_the_producers_key(self, tmp_path: Path) -> None:
        note = _prose_note().replace(
            "---\n\n# Pricing",
            f'correspondent_name: "{_DISPLAY}"\n---\n\n# Pricing',
        )
        raw = _raw(tmp_path / "raw" / "voltaire", note)
        ref = correspondent_from_raw(raw)
        assert ref.address == _ADDRESS
        assert ref.display_name == _DISPLAY
        assert ref.observed_at == _OBSERVED_AT

    def test_a_note_without_the_key_yields_blanks(self, tmp_path: Path) -> None:
        raw = _raw(tmp_path / "raw" / "sessions", _prose_note(with_key=False))
        assert correspondent_from_raw(raw).address == ""

    def test_none_yields_blanks(self) -> None:
        assert correspondent_from_raw(None).address == ""


# ---------------------------------------------------------------------------
# End to end through athenaeum.librarian.process_one
# ---------------------------------------------------------------------------


def _update_of(uid: str, name: str) -> ClassifiedEntity:
    return ClassifiedEntity(
        name=name,
        entity_type="person",
        tags=[],
        access="internal",
        is_new=False,
        existing_uid=uid,
        observations="They asked about tiered pricing before the renewal.",
    )


def _compile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    classified: list[ClassifiedEntity],
    note: str | None = None,
    seed_contact: bool = True,
) -> tuple[Path, object, RawFile]:
    """One prose note through ``process_one``, Tier 2/3 stubbed, no network.

    Seeds Dana's page AND her contact record, so the correspondent address
    resolves uniquely through the same lookup the corrections path uses — the
    premise athenaeum#1949's AC1 is stated against. Also seeds Robin's page as
    the decoy an observed misroute landed on.
    """
    monkeypatch.setenv("ATHENAEUM_CACHE_DIR", str(tmp_path / "cache"))
    knowledge = tmp_path / "knowledge"
    wiki = knowledge / "wiki"
    _write_page(wiki, _DANA_UID, name=_DISPLAY)
    _write_page(wiki, _ROBIN_UID, name="Robin Vance")
    if seed_contact:
        _write_record(knowledge, "dana-contact.md", uid=_DANA_UID, emails=[_ADDRESS])

    raw = _raw(knowledge / "raw" / "voltaire", _prose_note() if note is None else note)

    monkeypatch.setattr(
        "athenaeum.librarian.tier2_classify", lambda *_a, **_k: list(classified)
    )

    classify_client = MagicMock()
    classify_client.messages.create.side_effect = AssertionError(
        "tier2_classify is monkeypatched — the real classify client must never be called"
    )

    # A valid patch-mode merge response (the `ops` shape
    # `tiers.parse_merge_ops_response` expects) — one appended line citing the
    # raw file, which is what a correctly-routed summary looks like on disk.
    merge_text = json.dumps(
        {
            "ops": [
                {
                    "op": "append_section",
                    "text": (
                        "- They asked about tiered pricing before the "
                        f"renewal.[^1]\n\n[^1]: voltaire/{raw.path.name}"
                    ),
                }
            ],
            "adds_new_claim": True,
        }
    )

    def _write_response(*_a: object, **_k: object) -> MagicMock:
        response = MagicMock()
        block = MagicMock()
        block.text = merge_text
        response.content = [block]
        response.stop_reason = "end_turn"
        return response

    write_client = MagicMock()
    write_client.messages.create.side_effect = _write_response

    result = process_one(
        raw,
        EntityIndex(wiki),
        wiki,
        classify_client,
        valid_types=VALID_TYPES,
        valid_tags=[],
        valid_access=VALID_ACCESS,
        usage=TokenUsage(),
        config=dict(EXCLUDED_CONFIG),
        write_client=write_client,
    )
    return knowledge, result, raw


class TestCorrectPage:
    def test_the_summary_lands_on_the_correspondents_page(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        knowledge, result, raw = _compile(
            tmp_path, monkeypatch, classified=[_update_of(_DANA_UID, _DISPLAY)]
        )
        assert result.updated == [_DANA_UID]  # type: ignore[attr-defined]
        assert result.misrouted_updates_prevented == 0  # type: ignore[attr-defined]
        dana = (knowledge / "wiki" / f"{_DANA_UID}.md").read_text(encoding="utf-8")
        assert raw.path.name in dana, "the correspondent's page cites the raw file"
        # Robin's page is untouched.
        robin = (knowledge / "wiki" / f"{_ROBIN_UID}.md").read_text(encoding="utf-8")
        assert raw.path.name not in robin


class TestWrongPageMisroute:
    def test_a_write_to_another_person_is_refused_and_escalated(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC1's defect case, exactly as observed on the live store."""
        knowledge = tmp_path / "knowledge"
        robin_path = knowledge / "wiki" / f"{_ROBIN_UID}.md"

        _, result, raw = _compile(
            tmp_path, monkeypatch, classified=[_update_of(_ROBIN_UID, "Robin Vance")]
        )

        assert result.misrouted_updates_prevented == 1  # type: ignore[attr-defined]
        # The refused write is never counted as an update...
        assert result.updated == []  # type: ignore[attr-defined]
        # ...and the unrelated person's page is byte-for-byte untouched.
        robin = robin_path.read_text(encoding="utf-8")
        assert raw.path.name not in robin
        assert "tiered pricing" not in robin
        assert robin == (
            f"---\nuid: {_ROBIN_UID}\nname: Robin Vance\ntype: person\n"
            "created: 2026-01-01\nupdated: 2026-01-01\n---\n\n"
            "Notes about Robin Vance.\n"
        )
        # And it escalates with its reason.
        assert any(
            _DANA_UID in item.description and "received no write" in item.description
            for item in result.escalated  # type: ignore[attr-defined]
        ), result.escalated  # type: ignore[attr-defined]

    def test_an_unresolvable_correspondent_leaves_the_write_alone(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC1's premise is a correspondent that resolves UNIQUELY.

        With no contact record the address resolves to nothing, so the guard
        is inactive and behaviour is exactly what it is today — the guard must
        never refuse a write on a guess.
        """
        knowledge, result, raw = _compile(
            tmp_path,
            monkeypatch,
            classified=[_update_of(_ROBIN_UID, "Robin Vance")],
            seed_contact=False,
        )
        assert result.misrouted_updates_prevented == 0  # type: ignore[attr-defined]
        assert result.updated == [_ROBIN_UID]  # type: ignore[attr-defined]
        robin = (knowledge / "wiki" / f"{_ROBIN_UID}.md").read_text(encoding="utf-8")
        assert raw.path.name in robin

    def test_a_note_naming_no_correspondent_leaves_the_write_alone(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        knowledge, result, _ = _compile(
            tmp_path,
            monkeypatch,
            classified=[_update_of(_ROBIN_UID, "Robin Vance")],
            note=_prose_note(with_key=False),
        )
        assert result.misrouted_updates_prevented == 0  # type: ignore[attr-defined]
        assert result.updated == [_ROBIN_UID]  # type: ignore[attr-defined]


class TestMultiPersonCompilationStillWorks:
    def test_a_second_person_is_logged_not_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        knowledge, result, raw = _compile(
            tmp_path,
            monkeypatch,
            classified=[
                _update_of(_DANA_UID, _DISPLAY),
                _update_of(_ROBIN_UID, "Robin Vance"),
            ],
        )
        assert result.misrouted_updates_prevented == 0  # type: ignore[attr-defined]
        assert sorted(result.updated) == sorted([_DANA_UID, _ROBIN_UID])  # type: ignore[attr-defined]
        robin = (knowledge / "wiki" / f"{_ROBIN_UID}.md").read_text(encoding="utf-8")
        assert raw.path.name in robin, "a legitimate second-person write still lands"


class TestRouteLedger:
    def test_the_written_uids_are_recorded_for_the_raw_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC2 — runs logged counts only; now the per-file uid join is durable."""
        _, _, raw = _compile(
            tmp_path, monkeypatch, classified=[_update_of(_DANA_UID, _DISPLAY)]
        )
        rows = read_compile_routes(tmp_path / "cache")
        assert len(rows) == 1
        row = rows[0]
        assert row["raw_ref"] == raw.ref
        assert row["source"] == "voltaire"
        assert row["written_uids"] == [_DANA_UID]
        assert row["updated_uids"] == [_DANA_UID]
        assert row["correspondent_uid"] == _DANA_UID
        assert row["prevented_uids"] == []
        assert row["recorded_at"]

    def test_a_misroute_is_reconstructible_from_the_ledger_alone(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The exact reconstruction the rotated log could not provide."""
        _compile(
            tmp_path, monkeypatch, classified=[_update_of(_ROBIN_UID, "Robin Vance")]
        )
        rows = read_compile_routes(tmp_path / "cache")
        assert len(rows) == 1
        row = rows[0]
        # Nothing was written for this file at all -- the observed symptom.
        assert row["written_uids"] == []
        # And the ledger names both the intended page and the refused one.
        assert row["correspondent_uid"] == _DANA_UID
        assert row["prevented_uids"] == [_ROBIN_UID]

    def test_a_legitimate_second_person_write_is_recorded_as_logged(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _compile(
            tmp_path,
            monkeypatch,
            classified=[
                _update_of(_DANA_UID, _DISPLAY),
                _update_of(_ROBIN_UID, "Robin Vance"),
            ],
        )
        row = read_compile_routes(tmp_path / "cache")[0]
        assert sorted(row["written_uids"]) == sorted([_DANA_UID, _ROBIN_UID])
        assert row["logged_uids"] == [_ROBIN_UID]
        assert row["prevented_uids"] == []

    def test_a_row_is_written_even_for_a_file_with_no_correspondent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC2 is per raw file, not per conversation note."""
        _, _, raw = _compile(
            tmp_path,
            monkeypatch,
            classified=[_update_of(_ROBIN_UID, "Robin Vance")],
            note=_prose_note(with_key=False),
        )
        row = read_compile_routes(tmp_path / "cache")[0]
        assert row["raw_ref"] == raw.ref
        assert row["written_uids"] == [_ROBIN_UID]
        assert row["correspondent_uid"] == ""

    def test_the_ledger_lives_outside_the_corpus(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        knowledge, _, _ = _compile(
            tmp_path, monkeypatch, classified=[_update_of(_DANA_UID, _DISPLAY)]
        )
        assert read_compile_routes(tmp_path / "cache")
        assert not list(knowledge.rglob("_compile_route_records.jsonl"))


# ---------------------------------------------------------------------------
# The fail-open contracts this module documents
# ---------------------------------------------------------------------------


class TestFailOpen:
    """Every one of these is a documented guarantee, so each gets an assertion.

    The compile's wiki writes land before this module is consulted, so nothing
    here may raise: an audit trail is not a gate.
    """

    def test_malformed_frontmatter_yields_blanks_rather_than_raising(
        self, tmp_path: Path
    ) -> None:
        raw = _raw(tmp_path / "raw" / "voltaire", "---\n: : :\nnot: [yaml\n---\n\nbody\n")
        ref = correspondent_from_raw(raw)
        assert ref.address == ""

    def test_a_note_with_no_frontmatter_at_all_yields_blanks(
        self, tmp_path: Path
    ) -> None:
        raw = _raw(tmp_path / "raw" / "voltaire", "Just prose, no frontmatter.\n")
        assert correspondent_from_raw(raw).address == ""

    def test_observed_at_falls_back_to_the_raw_timestamp(self, tmp_path: Path) -> None:
        raw_dir = tmp_path / "raw" / "voltaire"
        raw_dir.mkdir(parents=True)
        path = raw_dir / "summary.md"
        path.write_text(
            f'---\n{CORRESPONDENT_EMAIL_KEY}: "{_ADDRESS}"\n---\n\nbody\n',
            encoding="utf-8",
        )
        raw = RawFile(
            path=path, source="voltaire", timestamp="20260930T110400Z", uuid8="aabbccdd"
        )
        assert correspondent_from_raw(raw).observed_at == "20260930T110400Z"

    def test_page_uid_of_a_missing_file_is_blank(self, tmp_path: Path) -> None:
        assert page_uid(tmp_path / "nope.md") == ""

    def test_page_uid_of_a_page_without_a_uid_is_blank(self, tmp_path: Path) -> None:
        path = tmp_path / "p.md"
        path.write_text("---\ntype: person\n---\n\nbody\n", encoding="utf-8")
        assert page_uid(path) == ""

    def test_an_unresolvable_address_disables_the_guard_rather_than_raising(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A resolution that EXPLODES must disable the guard, not fail the compile."""

        def _boom(*_a: object, **_k: object) -> None:
            raise RuntimeError("contacts surface unreadable")

        monkeypatch.setattr("athenaeum.corrections.resolve_target", _boom)
        wiki = tmp_path / "knowledge" / "wiki"
        _write_page(wiki, _DANA_UID, name=_DISPLAY)
        raw = _raw(tmp_path / "knowledge" / "raw" / "voltaire", _prose_note())
        assert (
            resolved_correspondent_uid(
                raw,
                index=EntityIndex(wiki),
                wiki_root=wiki,
                config=dict(EXCLUDED_CONFIG),
            )
            == ""
        )

    def test_a_ledger_write_failure_is_swallowed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(*_a: object, **_k: object) -> None:
            raise OSError("cache dir is read-only")

        monkeypatch.setattr("athenaeum.compile_routing.append_line_durable", _boom)
        # Must not raise.
        record_compile_route(
            CompileRouteRecord(raw_ref="voltaire/summary.md", source="voltaire"),
            cache_dir=tmp_path / "cache",
        )
        assert read_compile_routes(tmp_path / "cache") == []

    def test_a_malformed_ledger_row_does_not_make_the_trail_unreadable(
        self, tmp_path: Path
    ) -> None:
        cache = tmp_path / "cache"
        record_compile_route(
            CompileRouteRecord(raw_ref="voltaire/good.md", source="voltaire"),
            cache_dir=cache,
        )
        path = compile_route_ledger_path(cache)
        with path.open("a", encoding="utf-8") as handle:
            handle.write("\n")  # blank line
            handle.write("{not json\n")  # torn row
            handle.write('"a bare string"\n')  # valid JSON, wrong shape
        rows = read_compile_routes(cache)
        assert [row["raw_ref"] for row in rows] == ["voltaire/good.md"]

    def test_a_missing_ledger_reads_as_empty(self, tmp_path: Path) -> None:
        assert read_compile_routes(tmp_path / "never-written") == []
