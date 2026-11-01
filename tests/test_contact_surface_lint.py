# SPDX-License-Identifier: Apache-2.0
"""``athenaeum contact-surface-lint`` (issue athenaeum#2218).

Design ``docs/design/attribution-verification.md`` §10.4. One fixture store
(:func:`_knowledge`) covers all three base findings at once (the issue's own
AC: "a fixture store with one example of each covers it"):

- ``orphan-uid``: a contact record whose uid has no wiki page and no merge
  record -- ``pageless_records``.
- ``shared@example.org``: listed on both ``page-one`` and ``page-two`` --
  ``shared_values``.
- ``obs@example.com``: classified ``observed`` on ``page-one``, with a
  cited raw-file source that EXISTS but does not contain the value --
  ``observed_source_mismatch``.

Every address is ``example.org``/``example.com`` (issue AC7). Driven
through ``athenaeum.cli.main`` so the argparse wiring and exit codes are
exercised on the real path an operator takes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from athenaeum import pii, pii_attribution
from athenaeum.cli import main
from athenaeum.models import render_frontmatter
from tests.conftest import FakeLLMClient

_EXCLUDED_CONFIG_YAML = "storage:\n  mapping:\n    pii: excluded\n"

_PAGE_ONE_UID = "aaaaaaaa"
_PAGE_TWO_UID = "bbbbbbbb"
_ORPHAN_UID = "cccccccc"

_SHARED_VALUE = "shared@example.org"
_OBSERVED_VALUE = "obs@example.com"
_PLAIN_VALUE = "plain@example.org"


def _write_page(wiki_root: Path, *, uid: str, name: str) -> Path:
    wiki_root.mkdir(parents=True, exist_ok=True)
    path = wiki_root / f"{uid}-{name.lower().replace(' ', '-')}.md"
    path.write_text(
        render_frontmatter({"uid": uid, "name": name, "type": "person"})
        + "\nSynthetic page.\n",
        encoding="utf-8",
    )
    return path


def _write_record(
    contacts_root: Path,
    filename: str,
    *,
    uid: str,
    emails: list[str] | None = None,
    contact_classification: list[dict[str, Any]] | None = None,
) -> Path:
    contacts_root.mkdir(parents=True, exist_ok=True)
    meta: dict[str, Any] = {"uid": uid, "pii": True, "emails": list(emails or [])}
    if contact_classification is not None:
        meta["contact_classification"] = contact_classification
    path = contacts_root / filename
    path.write_text(
        render_frontmatter(meta) + "\nSynthetic contact record.\n", encoding="utf-8"
    )
    return path


@pytest.fixture
def _knowledge(tmp_path: Path) -> Path:
    root = tmp_path / "knowledge"
    wiki = root / "wiki"
    wiki.mkdir(parents=True)
    (root / "athenaeum.yaml").write_text(_EXCLUDED_CONFIG_YAML, encoding="utf-8")

    _write_page(wiki, uid=_PAGE_ONE_UID, name="Page One")
    _write_page(wiki, uid=_PAGE_TWO_UID, name="Page Two")

    contacts_root = root / "excluded"

    # Cited raw source exists but does not mention the value -- the
    # observed_source_mismatch fixture.
    raw_dir = root / "raw" / "intake"
    raw_dir.mkdir(parents=True)
    (raw_dir / "note.md").write_text("Hello there, nothing to see here.\n", encoding="utf-8")

    _write_record(
        contacts_root,
        f"{_PAGE_ONE_UID}-page-one.md",
        uid=_PAGE_ONE_UID,
        emails=[_SHARED_VALUE, _OBSERVED_VALUE],
        contact_classification=[
            {
                "identifier": _OBSERVED_VALUE,
                "usage_class": pii.USAGE_CLASS_OBSERVED,
                # Absolute path: `resolve_source_value_presence` reads the
                # path exactly as given (it does not search `raw/`/`wiki/`
                # under a different root), same as every real writer's
                # own `source_ref` already is in production.
                "source": str(raw_dir / "note.md"),
                "observed_at": "2026-10-01T00:00:00Z",
            }
        ],
    )
    _write_record(
        contacts_root,
        f"{_PAGE_TWO_UID}-page-two.md",
        uid=_PAGE_TWO_UID,
        emails=[_SHARED_VALUE, _PLAIN_VALUE],
    )
    _write_record(contacts_root, "orphan.md", uid=_ORPHAN_UID, emails=["lonely@example.org"])

    return root


def _run(*args: str) -> int:
    return main(["contact-surface-lint", *args])


class TestBaseReport:
    """AC1: read-only, reads only through ``pii``, counts/uids only."""

    def test_pageless_records_lists_the_orphan_uid(
        self, _knowledge: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = _run("--path", str(_knowledge))
        out = capsys.readouterr().out
        assert code == 1  # findings
        assert "pageless_records: 1" in out
        assert _ORPHAN_UID in out

    def test_shared_values_reports_both_uids_unsourced(
        self, _knowledge: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _run("--path", str(_knowledge))
        out = capsys.readouterr().out
        assert "shared_values: 1" in out
        assert "unsourced: 2" in out
        assert _PAGE_ONE_UID in out and _PAGE_TWO_UID in out

    def test_observed_source_mismatch_names_the_uid_and_source(
        self, _knowledge: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _run("--path", str(_knowledge))
        out = capsys.readouterr().out
        assert "observed_source_mismatch: 1" in out
        assert f"uid={_PAGE_ONE_UID}" in out
        assert "note.md" in out

    def test_no_fixture_value_appears_in_text_output(
        self, _knowledge: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _run("--path", str(_knowledge))
        out = capsys.readouterr().out
        for value in (_SHARED_VALUE, _OBSERVED_VALUE, _PLAIN_VALUE, "lonely@example.org"):
            assert value not in out

    def test_no_fixture_value_appears_in_json_output(
        self, _knowledge: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _run("--path", str(_knowledge), "--json")
        out = capsys.readouterr().out
        for value in (_SHARED_VALUE, _OBSERVED_VALUE, _PLAIN_VALUE, "lonely@example.org"):
            assert value not in out
        payload = json.loads(out)
        assert payload["counts"]["pageless_records"] == 1
        assert payload["counts"]["shared_values"] == 1
        assert payload["counts"]["observed_source_mismatch"] == 1

    def test_clean_surface_exits_zero(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root = tmp_path / "knowledge"
        wiki = root / "wiki"
        wiki.mkdir(parents=True)
        (root / "athenaeum.yaml").write_text(_EXCLUDED_CONFIG_YAML, encoding="utf-8")
        _write_page(wiki, uid=_PAGE_ONE_UID, name="Page One")
        contacts_root = root / "excluded"
        _write_record(
            contacts_root, f"{_PAGE_ONE_UID}-page-one.md", uid=_PAGE_ONE_UID, emails=[_PLAIN_VALUE]
        )
        code = _run("--path", str(root))
        out = capsys.readouterr().out
        assert code == 0
        assert "OK" in out

    def test_pageless_uid_is_not_a_shared_values_holder(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """PR review finding 1 (second half): a value held by one live uid and
        one pageless uid is NOT a shared value -- seam D2's "no live page does
        not count" rule. The pageless uid is still reported, once, under
        ``pageless_records``."""
        root = tmp_path / "knowledge"
        wiki = root / "wiki"
        wiki.mkdir(parents=True)
        (root / "athenaeum.yaml").write_text(_EXCLUDED_CONFIG_YAML, encoding="utf-8")
        _write_page(wiki, uid=_PAGE_ONE_UID, name="Page One")
        contacts_root = root / "excluded"
        _write_record(
            contacts_root,
            f"{_PAGE_ONE_UID}-page-one.md",
            uid=_PAGE_ONE_UID,
            emails=[_SHARED_VALUE],
        )
        _write_record(contacts_root, "orphan.md", uid=_ORPHAN_UID, emails=[_SHARED_VALUE])

        code = _run("--path", str(root), "--json")
        payload = json.loads(capsys.readouterr().out)
        assert code == 1  # the pageless record is still a finding
        assert payload["counts"]["pageless_records"] == 1
        assert payload["counts"]["shared_values"] == 0

    def test_unmapped_pii_class_is_an_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root = tmp_path / "knowledge"
        (root / "wiki").mkdir(parents=True)
        # No athenaeum.yaml -- `pii` is not mapped to an excluded surface.
        code = _run("--path", str(root))
        err = capsys.readouterr().err
        assert code == 2
        assert "not mapped" in err


class TestAttributionReportMode:
    """AC2: report mode over every (value, uid) pair, writer=lint."""

    def test_writes_only_attribution_log_rows_marked_writer_lint(
        self, _knowledge: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        contacts_root = _knowledge / "excluded"
        before = {
            p.relative_to(contacts_root).as_posix(): p.read_bytes()
            for p in sorted(contacts_root.rglob("*"))
            if p.is_file()
        }

        code = _run("--path", str(_knowledge), "--attribution")
        out = capsys.readouterr().out
        assert code == 1

        log_path = pii_attribution.default_attribution_log_path(contacts_root)
        rows = [
            json.loads(line)
            for line in log_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert rows
        assert all(r["writer"] == pii_attribution.WRITER_LINT for r in rows)

        # Every pre-existing record file is untouched -- the seam parks
        # nothing on this module's behalf (design §4.1).
        after = {
            p.relative_to(contacts_root).as_posix(): p.read_bytes()
            for p in sorted(contacts_root.rglob("*"))
            if p.is_file()
        }
        for rel, content in before.items():
            assert after[rel] == content

        assert "by disposition" in out
        assert "by check" in out
        assert "by question class" in out

    def test_no_fixture_value_in_attribution_output(
        self, _knowledge: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _run("--path", str(_knowledge), "--attribution")
        out = capsys.readouterr().out
        for value in (_SHARED_VALUE, _OBSERVED_VALUE, _PLAIN_VALUE):
            assert value not in out

    def test_duplicate_record_for_one_uid_counts_the_pair_once(
        self, tmp_path: Path
    ) -> None:
        """PR review finding 1 (first half): one logical (value, uid) pair,
        even when the SAME uid has two record FILES listing it."""
        root = tmp_path / "knowledge"
        wiki = root / "wiki"
        wiki.mkdir(parents=True)
        (root / "athenaeum.yaml").write_text(_EXCLUDED_CONFIG_YAML, encoding="utf-8")
        _write_page(wiki, uid=_PAGE_ONE_UID, name="Page One")
        contacts_root = root / "excluded"
        dup_value = "dup@example.org"
        _write_record(
            contacts_root, "dup-a.md", uid=_PAGE_ONE_UID, emails=[dup_value]
        )
        _write_record(
            contacts_root, "dup-b.md", uid=_PAGE_ONE_UID, emails=[dup_value]
        )

        code = _run("--path", str(root), "--attribution")
        assert code in (0, 1)

        log_path = pii_attribution.default_attribution_log_path(contacts_root)
        rows = [
            json.loads(line)
            for line in log_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert len(rows) == 1


class TestPersistedObserveReport:
    """AC3: one observe-report record per run, never a value."""

    def test_report_shape(self, _knowledge: Path) -> None:
        _run("--path", str(_knowledge), "--attribution")
        contacts_root = _knowledge / "excluded"
        reports = pii_attribution.read_attribution_observe_reports(contacts_root)
        assert len(reports) == 1
        report = reports[0]
        assert report.report_id
        assert report.ts
        assert set(report.model_by_class) == set(pii_attribution.QUESTION_CLASSES)
        assert set(report.prompt_sha256_by_class) == set(pii_attribution.QUESTION_CLASSES)
        # The shared value triggers D2 (held-elsewhere) on both holders.
        assert report.per_class_shadow_counts.get(
            pii_attribution.QUESTION_CLASS_HELD_ELSEWHERE, 0
        ) >= 1

    def test_report_never_holds_a_value(self, _knowledge: Path) -> None:
        _run("--path", str(_knowledge), "--attribution")
        contacts_root = _knowledge / "excluded"
        raw = pii_attribution.default_attribution_observe_report_path(
            contacts_root
        ).read_text(encoding="utf-8")
        for value in (_SHARED_VALUE, _OBSERVED_VALUE, _PLAIN_VALUE):
            assert value not in raw

    def test_second_run_appends_a_second_report(self, _knowledge: Path) -> None:
        _run("--path", str(_knowledge), "--attribution")
        _run("--path", str(_knowledge), "--attribution")
        contacts_root = _knowledge / "excluded"
        reports = pii_attribution.read_attribution_observe_reports(contacts_root)
        assert len(reports) == 2
        assert reports[0].report_id != reports[1].report_id


class TestEscalate:
    """AC4: opt-in shadow escalation, capped, memo-respecting."""

    def _park_shadow_suspect(self, knowledge_root: Path) -> None:
        context = pii_attribution.build_attribution_context(
            knowledge_root, {"storage": {"mapping": {"pii": "excluded"}}}
        )
        verdict = pii_attribution.AttributionVerdict(
            disposition=pii_attribution.DISPOSITION_SUSPECT,
            checks=(
                pii_attribution.CheckEvidence(
                    check_id=pii_attribution.CHECK_D2_HELD_ELSEWHERE,
                    uids=(_PAGE_TWO_UID,),
                ),
            ),
            candidates=(_PAGE_TWO_UID,),
            value_key=pii_attribution.value_key_for(context, _SHARED_VALUE),
        )
        pii_attribution.park_suspect(
            context,
            _SHARED_VALUE,
            verdict,
            target_uid=_PAGE_ONE_UID,
            target_name="Page One",
            writer=pii_attribution.WRITER_OBSERVED_CONTACT,
            source_ref="raw/intake/note.md",
            shadow=True,
        )

    def test_escalate_without_attribution_is_rejected(
        self, _knowledge: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = _run("--path", str(_knowledge), "--escalate")
        err = capsys.readouterr().err
        assert code == 2
        assert "--attribution" in err

    def test_escalate_with_no_configured_client_errors_without_side_effects(
        self,
        _knowledge: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """PR review finding 2: a `None` client must not silently no-op."""
        monkeypatch.setattr(
            "athenaeum._cmd_contact_surface_lint.build_llm_client",
            lambda *a, **k: None,
        )
        contacts_root = _knowledge / "excluded"
        log_path = pii_attribution.default_attribution_log_path(contacts_root)
        report_path = pii_attribution.default_attribution_observe_report_path(contacts_root)

        code = _run("--path", str(_knowledge), "--attribution", "--escalate")
        err = capsys.readouterr().err

        assert code == 2
        assert "ANTHROPIC_API_KEY" in err or "LLM client" in err
        # No attribution sweep ran at all: no log, no observe report.
        assert not log_path.exists()
        assert not report_path.exists()

    def test_second_run_makes_zero_calls(
        self,
        _knowledge: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        self._park_shadow_suspect(_knowledge)
        client = FakeLLMClient(text=json.dumps({"verdict": "own", "score": 0.95}))
        monkeypatch.setattr(
            "athenaeum._cmd_contact_surface_lint.build_llm_client",
            lambda *a, **k: client,
        )

        _run("--path", str(_knowledge), "--attribution", "--escalate")
        first_calls = len(client.calls)
        assert first_calls >= 1

        _run("--path", str(_knowledge), "--attribution", "--escalate")
        second_calls = len(client.calls)
        assert second_calls == first_calls  # zero NEW calls on the second run

        out = capsys.readouterr().out
        for value in (_SHARED_VALUE, _OBSERVED_VALUE, _PLAIN_VALUE):
            assert value not in out


class TestCollisionSummaryHint:
    """AC6: the hint resolves to the real, registered command."""

    def test_hint_names_the_real_command(self) -> None:
        import logging

        from athenaeum.cli import _SUBCOMMAND_LOADERS

        assert "contact-surface-lint" in _SUBCOMMAND_LOADERS

        records: list[logging.LogRecord] = []

        class _Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record)

        handler = _Capture()
        logger = logging.getLogger("athenaeum.pii")
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        try:
            pii._log_collision_summary(1, 0)
        finally:
            logger.removeHandler(handler)

        assert records
        message = records[0].getMessage()
        assert "athenaeum contact-surface-lint" in message
